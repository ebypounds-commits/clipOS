"""Tests for the hardened deployment topology contracts (CLIP-094).

Covers three layers:

1. the pure invariant functions in ``clipos_api.deployment_topology``,
   including deliberate-regression cases so a broken invariant is proven
   to FAIL rather than silently pass;
2. the ``scripts/deploy_topology_check.py`` harness end to end;
3. the real repository topologies — ``deploy/compose.staging.yml`` and
   ``deploy/compose.production.yml`` must satisfy every hardening
   invariant and mirror each other's service set.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from clipos_api import deployment_topology as dt


def _find_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "10_TICKETS.yaml").exists():
            return candidate
    return Path(__file__).resolve().parents[3]


REPO_ROOT = _find_repo_root()
STAGING = REPO_ROOT / "deploy" / "compose.staging.yml"
PRODUCTION = REPO_ROOT / "deploy" / "compose.production.yml"


def _minimal_hardened() -> dict:
    """A topology that passes every invariant, for mutation testing."""
    return {
        "services": {
            "api": {
                "image": "${CLIPOS_API_IMAGE}",
                "restart": "unless-stopped",
                "environment": {
                    "DATABASE_URL": "${DATABASE_URL}",
                    "STORAGE_SECRET_KEY": "${STORAGE_SECRET_KEY}",
                },
                "healthcheck": {"test": ["CMD", "true"]},
                "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "1g"}}},
            },
            "postgres": {
                "image": "pgvector/pgvector:pg16",
                "restart": "unless-stopped",
                "env_file": ["deploy/.env"],
                "healthcheck": {"test": ["CMD", "true"]},
                "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "1g"}}},
            },
        }
    }


# ---------------------------------------------------------------------------
# Pure invariant functions
# ---------------------------------------------------------------------------


def test_minimal_hardened_topology_passes() -> None:
    report = dt.validate_topology(_minimal_hardened(), "production", "inline")
    assert report.summary["failed"] == 0, [
        f.evidence for f in report.findings if f.status == "FAIL"
    ]
    assert report.verdict == "PASS"


def test_internal_service_with_host_port_fails_h1() -> None:
    compose = _minimal_hardened()
    compose["services"]["postgres"]["ports"] = ["5432:5432"]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H1" in failing


def test_dev_credential_marker_fails_h2() -> None:
    compose = _minimal_hardened()
    compose["services"]["postgres"]["environment"] = {"POSTGRES_PASSWORD": "clipos_dev_local_only"}
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H2" in failing


def test_missing_restart_policy_fails_h3() -> None:
    compose = _minimal_hardened()
    del compose["services"]["api"]["restart"]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H3" in failing


def test_missing_healthcheck_fails_h4() -> None:
    compose = _minimal_hardened()
    del compose["services"]["api"]["healthcheck"]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H4" in failing


def test_one_shot_init_is_exempt_from_long_running_invariants() -> None:
    compose = _minimal_hardened()
    compose["services"]["migrate"] = {
        "restart": "no",
        "labels": {"clipos.role": "one-shot-init"},
    }
    report = dt.validate_topology(compose, "production", "inline")
    # A one-shot init container runs once and exits: it is exempt from the
    # restart-policy, healthcheck and resource-limit invariants.
    exempt = {"H3", "H4", "H6"}
    for finding in report.findings:
        if finding.invariant in exempt:
            assert "migrate" not in finding.evidence, finding.evidence
    assert report.verdict == "PASS"


def test_privileged_service_fails_h5() -> None:
    compose = _minimal_hardened()
    compose["services"]["api"]["privileged"] = True
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H5" in failing


def test_docker_socket_mount_fails_h5() -> None:
    compose = _minimal_hardened()
    compose["services"]["api"]["volumes"] = ["/var/run/docker.sock:/var/run/docker.sock"]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H5" in failing


def test_missing_resource_limits_fails_h6() -> None:
    compose = _minimal_hardened()
    del compose["services"]["api"]["deploy"]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H6" in failing


def test_literal_secret_fails_h7() -> None:
    compose = _minimal_hardened()
    compose["services"]["api"]["environment"]["JEV_API_KEY"] = "sk-abcdefghijklmnopqrstuvwxyz012345"
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H7" in failing


def test_literal_reference_key_fails_h8() -> None:
    compose = _minimal_hardened()
    compose["services"]["api"]["environment"]["DATABASE_URL"] = (
        "postgresql+psycopg://user:pw@postgres:5432/db"
    )
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H8" in failing


def test_environment_list_form_is_understood_by_h8() -> None:
    compose = _minimal_hardened()
    compose["services"]["api"]["environment"] = [
        "DATABASE_URL=postgresql+psycopg://u:p@postgres:5432/db",
    ]
    report = dt.validate_topology(compose, "production", "inline")
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H8" in failing


def test_report_is_json_serializable() -> None:
    report = dt.validate_topology(_minimal_hardened(), "production", "inline")
    json.dumps(report.model_dump(mode="json"))


def test_raw_text_drives_the_literal_secret_scan() -> None:
    """A secret in the raw file must FAIL even if the mapping is clean."""
    compose = _minimal_hardened()
    report = dt.validate_topology(
        compose,
        "production",
        "inline",
        raw_text="JEV_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345",
    )
    failing = {f.invariant for f in report.findings if f.status == "FAIL"}
    assert "H7" in failing


def _load_yaml(path: Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_load_compose_rejects_scalar_services(tmp_path: Path) -> None:
    harness = _load_harness()
    bad = tmp_path / "bad.yml"
    bad.write_text("services: not-a-mapping\n", encoding="utf-8")
    with pytest.raises(TypeError):
        harness.load_compose(bad)


# ---------------------------------------------------------------------------
# The repository's real topologies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "environment"),
    [(STAGING, "staging"), (PRODUCTION, "production")],
)
def test_repo_topology_passes_every_invariant(path: Path, environment: str) -> None:
    assert path.exists(), f"missing topology file: {path}"
    raw_text = path.read_text(encoding="utf-8")
    report = dt.validate_topology(_load_yaml(path), environment, str(path), raw_text)
    failures = [f"{f.invariant}: {f.evidence}" for f in report.findings if f.status == "FAIL"]
    assert failures == [], f"{environment} topology FAILED: {failures}"
    assert report.verdict == "PASS"


def test_repo_topologies_define_the_same_services() -> None:
    staging = _load_yaml(STAGING)
    production = _load_yaml(PRODUCTION)
    assert set(staging["services"]) == set(production["services"])


def test_internal_services_are_not_host_published_in_either_topology() -> None:
    for path in (STAGING, PRODUCTION):
        compose = _load_yaml(path)
        for name in ("postgres", "redis", "minio", "temporal"):
            assert "ports" not in compose["services"][name], f"{name} publishes ports in {path}"


def test_internal_network_is_declared_internal() -> None:
    for path in (STAGING, PRODUCTION):
        compose = _load_yaml(path)
        internal = compose["networks"]["clipos_internal"]
        assert internal.get("internal") is True, f"clipos_internal not internal in {path}"


# ---------------------------------------------------------------------------
# The CLI harness
# ---------------------------------------------------------------------------


def _load_harness():
    spec = importlib.util.spec_from_file_location(
        "deploy_topology_check", REPO_ROOT / "scripts" / "deploy_topology_check.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["deploy_topology_check"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop("deploy_topology_check", None)
        raise
    return module


def test_harness_reports_pass_on_real_topologies() -> None:
    harness = _load_harness()
    report = harness.run_check()
    assert report["summary"]["verdict"] == "PASS"
    assert report["summary"]["mirror"] == "PASS"
    assert set(report["topologies"]) == {"staging", "production"}


def test_harness_main_exits_zero_on_real_topologies() -> None:
    harness = _load_harness()
    assert harness.main(["deploy_topology_check.py"]) == 0


def test_harness_writes_json_report(tmp_path: Path) -> None:
    harness = _load_harness()
    out = tmp_path / "topology.json"
    code = harness.main(["deploy_topology_check.py", "--json", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["tool"] == "clipos-deploy-topology-check"
    assert payload["version"] == 1
    for key in ("summary", "topologies", "mirror_evidence"):
        assert key in payload


def test_harness_fails_when_topology_drifts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A regressed production topology must flip the harness to FAIL."""
    harness = _load_harness()
    compose = _load_yaml(PRODUCTION)
    compose["services"]["postgres"]["ports"] = ["5432:5432"]
    bad = tmp_path / "compose.production.yml"
    import yaml

    bad.write_text(yaml.safe_dump(compose), encoding="utf-8")
    monkeypatch.setitem(harness.TOPOLOGIES, "production", bad)

    report = harness.run_check()
    assert report["summary"]["verdict"] == "FAIL"
    assert harness.main(["deploy_topology_check.py"]) == 1


def test_harness_detects_mirror_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    harness = _load_harness()
    staging = _load_yaml(STAGING)
    staging["services"]["extra-service"] = {
        "restart": "unless-stopped",
        "healthcheck": {"test": ["CMD", "true"]},
        "deploy": {"resources": {"limits": {"cpus": "1.0", "memory": "1g"}}},
    }
    bad = tmp_path / "compose.staging.yml"
    import yaml

    bad.write_text(yaml.safe_dump(staging), encoding="utf-8")
    monkeypatch.setitem(harness.TOPOLOGIES, "staging", bad)

    report = harness.run_check()
    assert report["summary"]["mirror"] == "FAIL"
    assert report["summary"]["verdict"] == "FAIL"


def test_harness_exits_two_on_unreadable_topology(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness = _load_harness()
    monkeypatch.setitem(harness.TOPOLOGIES, "production", tmp_path / "nope.yml")
    assert harness.main(["deploy_topology_check.py"]) == 2
