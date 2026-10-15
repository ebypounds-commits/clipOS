#!/usr/bin/env python3
"""Deployment topology check (CLIP-094).

Validates the staging and production Compose topologies against the
hardening invariants H1..H8 defined in `clipos_api.deployment_topology`,
and additionally cross-checks that the two files define the same set of
services (staging mirrors production).

The check is pure: it reads the Compose files and emits a machine-checkable
JSON report. It never contacts a Docker daemon, so it runs in CI.

Usage:
  python3 scripts/deploy_topology_check.py [--json PATH] [--strict] [--quiet]

Exit codes:
  0  every topology passed (warnings allowed unless --strict)
  1  at least one invariant FAILED, or staging/production drifted
  2  a topology file could not be read or parsed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_API_ROOT = Path(__file__).resolve().parents[1] / "services" / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from clipos_api.deployment_topology import (  # noqa: E402
    TopologyReport,
    validate_topology,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TOPOLOGIES = {
    "staging": REPO_ROOT / "deploy" / "compose.staging.yml",
    "production": REPO_ROOT / "deploy" / "compose.production.yml",
}


def load_compose(path: Path) -> tuple[dict, str]:
    """Load a Compose file, returning (mapping, raw text).

    The mapping is guaranteed to hold a ``services`` dict; the raw text is
    kept for the literal-secret scan so H7 sees exactly what is on disk.
    """
    text = path.read_text(encoding="utf-8")
    loaded = yaml.safe_load(text) or {}
    if not isinstance(loaded, dict):
        raise TypeError(f"compose file did not parse to a mapping: {path}")
    services = loaded.get("services")
    if services is None:
        loaded["services"] = {}
    elif not isinstance(services, dict):
        raise TypeError(f"compose 'services' must be a mapping: {path}")
    return loaded, text


def _check_mirror(staging: dict, production: dict) -> tuple[bool, str]:
    """Staging must define the same service set as production."""
    staging_services = set((staging.get("services") or {}).keys())
    production_services = set((production.get("services") or {}).keys())
    if staging_services != production_services:
        only_staging = sorted(staging_services - production_services)
        only_production = sorted(production_services - staging_services)
        return (
            False,
            f"staging-only={only_staging} production-only={only_production}",
        )
    return True, f"{len(production_services)} services match"


def run_check() -> dict:
    reports: dict[str, TopologyReport] = {}
    loaded: dict[str, dict] = {}
    for environment, path in TOPOLOGIES.items():
        compose, raw_text = load_compose(path)
        loaded[environment] = compose
        reports[environment] = validate_topology(compose, environment, str(path), raw_text)

    mirror_ok = True
    mirror_evidence = ""
    if "staging" in loaded and "production" in loaded:
        mirror_ok, mirror_evidence = _check_mirror(loaded["staging"], loaded["production"])

    failed = sum(1 for r in reports.values() if r.summary["failed"])
    warned = sum(1 for r in reports.values() if r.summary["warnings"])
    if not mirror_ok:
        failed += 1

    return {
        "tool": "clipos-deploy-topology-check",
        "version": 1,
        "summary": {
            "topologies": len(reports),
            "failed": failed,
            "warnings": warned,
            "mirror": "PASS" if mirror_ok else "FAIL",
            "verdict": "PASS" if failed == 0 else "FAIL",
        },
        "mirror_evidence": mirror_evidence,
        "topologies": {
            environment: report.model_dump(mode="json")
            for environment, report in reports.items()
        },
    }


def render(report: dict, verbose: bool = True) -> None:
    print("ClipOS deployment topology check")
    print()
    for environment, topology in report["topologies"].items():
        summary = topology["summary"]
        print(
            f"[{summary['verdict']:4}] {environment:11} "
            f"{summary['passed']} passed / {summary['warnings']} warn / "
            f"{summary['failed']} failed"
        )
        if verbose:
            for finding in topology["findings"]:
                print(
                    f"        [{finding['status']:4}] {finding['invariant']} "
                    f"{finding['title']}: {finding['evidence']}"
                )
    print()
    print(
        f"[{report['summary']['mirror']:4}] staging/production mirror: "
        f"{report['mirror_evidence']}"
    )
    print(f"\nVERDICT: {report['summary']['verdict']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="", help="write the report JSON here")
    parser.add_argument(
        "--strict", action="store_true", help="treat warnings as failures"
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv[1:])

    try:
        report = run_check()
    except (OSError, ValueError, TypeError) as exc:
        print(f"topology check could not run: {exc}", file=sys.stderr)
        return 2

    render(report, verbose=not args.quiet)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nreport written to {args.json}")

    if report["summary"]["failed"]:
        return 1
    if args.strict and report["summary"]["warnings"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
