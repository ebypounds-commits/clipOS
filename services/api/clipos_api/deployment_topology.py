"""Hardened deployment topology contracts and validation (CLIP-094).

ClipOS ships three Compose topologies:

* ``docker-compose.dev.yml``        — local development, non-production creds.
* ``deploy/compose.staging.yml``    — mirrors production shape, reduced resources.
* ``deploy/compose.production.yml`` — the hardened production topology.

The staging and production files must satisfy a fixed set of hardening
invariants so that a drift in either file is caught by CI rather than at
deploy time. Those invariants live here as pure functions over a parsed
Compose mapping, which keeps them testable without a Docker daemon:

  H1  no service uses a host-published port for an internal service
      (postgres/redis/minio/temporal must not be reachable from the host);
  H2  no service relies on a default or development credential;
  H3  every service declares a restart policy other than the dev ``"no"``;
  H4  every long-running service declares a healthcheck;
  H5  the application services do not mount the Docker socket or run
      privileged;
  H6  every service sets resource limits (cpu/memory) in hosting envs;
  H7  the topology's secrets are referenced by name only — no literal
      secret value is present in the file;
  H8  every application service receives its configuration through
      environment references, never baked into the image.

One-shot init containers (labelled ``clipos.role: one-shot-init``) run once
and exit by design, so they are exempt from H3, H4 and H6.

Validation returns a structured report so the CLI can print evidence and
exit non-zero on any FAIL.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Literal

Severity = Literal["FAIL", "WARN", "PASS"]

# Services that must never be published to the host in staging/production.
_INTERNAL_SERVICES = ("postgres", "redis", "minio", "temporal")

# Credentials that are acceptable only in the dev topology.
_DEV_CREDENTIAL_MARKERS = (
    "clipos_dev",
    "clipos_dev_local_only",
    "local_only",
)

_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# Environment keys whose *value* must be injected, not literalled.
_REFERENCE_ONLY_KEYS = (
    "DATABASE_URL",
    "CACHE_REDIS_URL",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "POSTGRES_PASSWORD",
    "MINIO_ROOT_PASSWORD",
    "JEV_API_KEY",
)


@dataclass
class TopologyFinding:
    """One hardening invariant observation over a topology file."""

    invariant: str
    title: str
    status: Severity
    evidence: str
    service: str | None = None


@dataclass
class TopologyReport:
    """Machine-checkable result of validating one Compose topology.

    Plain dataclasses (not Pydantic) on purpose: this module is imported by
    ``scripts/deploy_topology_check.py``, which runs in the dependency-light
    CI `validate` job where only PyYAML is installed.
    """

    environment: str
    path: str = ""
    services: list[str] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    findings: list[TopologyFinding] = field(default_factory=list)
    tool: str = "clipos-deploy-topology-check"
    version: int = 1
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def verdict(self) -> Severity:
        return "FAIL" if any(f.status == "FAIL" for f in self.findings) else "PASS"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["generated_at"] = self.generated_at.isoformat()
        return data


def _walk_strings(node: object) -> list[str]:
    """Collect every scalar in a nested mapping/sequence as a string."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append(str(key))
            found.extend(_walk_strings(value))
    elif isinstance(node, (list, tuple)):
        for item in node:
            found.extend(_walk_strings(item))
    elif node is not None:
        found.append(str(node))
    return found


def _published_ports(service: dict) -> list[str]:
    raw = service.get("ports") or []
    if not isinstance(raw, list):
        return []
    return [str(p) for p in raw]


def _has_literal_secret(text: str) -> str | None:
    for pattern in _SECRET_VALUE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)[:8] + "..."
    return None


def _check_internal_not_published(services: dict) -> list[TopologyFinding]:
    findings: list[TopologyFinding] = []
    offenders: list[str] = []
    for name, service in services.items():
        if name in _INTERNAL_SERVICES and _published_ports(service or {}):
            offenders.append(name)
    if offenders:
        findings.append(
            TopologyFinding(
                invariant="H1",
                title="internal services not host-published",
                status="FAIL",
                evidence=f"host-published internal service(s): {', '.join(sorted(offenders))}",
            )
        )
    else:
        findings.append(
            TopologyFinding(
                invariant="H1",
                title="internal services not host-published",
                status="PASS",
                evidence="no internal service publishes a host port",
            )
        )
    return findings


def _check_no_dev_credentials(services: dict) -> list[TopologyFinding]:
    offenders: list[str] = []
    for name, service in services.items():
        blob = "\n".join(_walk_strings(service or {}))
        for marker in _DEV_CREDENTIAL_MARKERS:
            if marker in blob:
                offenders.append(f"{name} ({marker})")
                break
    if offenders:
        return [
            TopologyFinding(
                invariant="H2",
                title="no development credentials",
                status="FAIL",
                evidence=f"development credential marker(s): {', '.join(sorted(offenders))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H2",
            title="no development credentials",
            status="PASS",
            evidence="no development credential markers present",
        )
    ]


def _is_one_shot(name: str, service: dict) -> bool:
    """One-shot init containers run once and exit by design.

    They are exempt from the long-running invariants (restart policy,
    healthcheck, resource limits): ``restart: "no"`` is correct for them,
    and they do not hold resources between runs.
    """
    labels = (service or {}).get("labels") or {}
    if isinstance(labels, list):
        return any("one-shot" in str(item) for item in labels)
    return "one-shot" in str(labels)


def _check_restart_policies(services: dict) -> list[TopologyFinding]:
    missing = [
        n
        for n, s in services.items()
        if not _is_one_shot(n, s or {}) and (s or {}).get("restart", "no") == "no"
    ]
    if missing:
        return [
            TopologyFinding(
                invariant="H3",
                title="restart policy set",
                status="FAIL",
                evidence=f"service(s) without a restart policy: {', '.join(sorted(missing))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H3",
            title="restart policy set",
            status="PASS",
            evidence="every service declares a non-'no' restart policy",
        )
    ]


def _check_healthchecks(services: dict) -> list[TopologyFinding]:
    missing = [
        n
        for n, s in services.items()
        if not _is_one_shot(n, s or {}) and "healthcheck" not in (s or {})
    ]
    if missing:
        return [
            TopologyFinding(
                invariant="H4",
                title="healthchecks declared",
                status="FAIL",
                evidence=f"service(s) without a healthcheck: {', '.join(sorted(missing))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H4",
            title="healthchecks declared",
            status="PASS",
            evidence="every long-running service declares a healthcheck",
        )
    ]


def _check_no_docker_socket(services: dict) -> list[TopologyFinding]:
    offenders: list[str] = []
    for name, service in services.items():
        service = service or {}
        if service.get("privileged"):
            offenders.append(f"{name} (privileged)")
            continue
        for volume in service.get("volumes") or []:
            if "docker.sock" in str(volume):
                offenders.append(f"{name} (docker.sock)")
                break
    if offenders:
        return [
            TopologyFinding(
                invariant="H5",
                title="no privileged access or docker socket mount",
                status="FAIL",
                evidence=f"offender(s): {', '.join(sorted(offenders))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H5",
            title="no privileged access or docker socket mount",
            status="PASS",
            evidence="no service is privileged and no docker.sock is mounted",
        )
    ]


def _check_resource_limits(services: dict) -> list[TopologyFinding]:
    missing: list[str] = []
    for name, service in services.items():
        service = service or {}
        if _is_one_shot(name, service):
            continue
        deploy = service.get("deploy") or {}
        limits = deploy.get("resources", {}).get("limits", {})
        if not limits.get("cpus") or not limits.get("memory"):
            missing.append(name)
    if missing:
        return [
            TopologyFinding(
                invariant="H6",
                title="resource limits declared",
                status="FAIL",
                evidence=f"service(s) without cpu/memory limits: {', '.join(sorted(missing))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H6",
            title="resource limits declared",
            status="PASS",
            evidence="every service declares cpu and memory limits",
        )
    ]


def _check_no_literal_secrets(text: str) -> list[TopologyFinding]:
    found = _has_literal_secret(text)
    if found:
        return [
            TopologyFinding(
                invariant="H7",
                title="no literal secret material",
                status="FAIL",
                evidence=f"secret-like literal present: {found}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H7",
            title="no literal secret material",
            status="PASS",
            evidence="no secret-like literal found in the topology file",
        )
    ]


def _check_env_references(services: dict) -> list[TopologyFinding]:
    """Reference-only keys must be `${VAR}` references, never literal values."""
    literal: list[str] = []
    for name, service in services.items():
        env = (service or {}).get("environment")
        env_keys: dict[str, object] = {}
        if isinstance(env, dict):
            env_keys = dict(env)
        elif isinstance(env, list):
            for item in env:
                text = str(item)
                if "=" in text:
                    key, _, raw_value = text.partition("=")
                    env_keys[key] = raw_value
                else:
                    env_keys[text] = None
        for key in _REFERENCE_ONLY_KEYS:
            if key in env_keys:
                injected = env_keys[key]
                if injected is not None and not str(injected).startswith("${"):
                    literal.append(f"{name}:{key}")
    if literal:
        return [
            TopologyFinding(
                invariant="H8",
                title="configuration injected by reference",
                status="FAIL",
                evidence=f"literal value for reference-only key(s): {', '.join(sorted(literal))}",
            )
        ]
    return [
        TopologyFinding(
            invariant="H8",
            title="configuration injected by reference",
            status="PASS",
            evidence="all reference-only keys use ${VAR} injection",
        )
    ]


def validate_topology(
    compose: dict, environment: str, path: str = "", raw_text: str = ""
) -> TopologyReport:
    """Validate every hardening invariant against a parsed Compose mapping.

    ``raw_text`` is the on-disk source of the topology, used for the H7
    literal-secret scan. When omitted, the structure is serialized back to
    a canonical text form so the scan still covers every scalar.
    """
    services: dict = compose.get("services") or {}
    text = raw_text or "\n".join(_walk_strings(compose))

    findings: list[TopologyFinding] = []
    findings.extend(_check_internal_not_published(services))
    findings.extend(_check_no_dev_credentials(services))
    findings.extend(_check_restart_policies(services))
    findings.extend(_check_healthchecks(services))
    findings.extend(_check_no_docker_socket(services))
    findings.extend(_check_resource_limits(services))
    findings.extend(_check_no_literal_secrets(text))
    findings.extend(_check_env_references(services))

    passed = sum(1 for f in findings if f.status == "PASS")
    failed = sum(1 for f in findings if f.status == "FAIL")
    warned = sum(1 for f in findings if f.status == "WARN")
    return TopologyReport(
        environment=environment,
        path=path,
        services=sorted(services),
        summary={
            "total": len(findings),
            "passed": passed,
            "warnings": warned,
            "failed": failed,
            "verdict": "PASS" if failed == 0 else "FAIL",
        },
        findings=findings,
    )


__all__ = [
    "Severity",
    "TopologyFinding",
    "TopologyReport",
    "validate_topology",
]
