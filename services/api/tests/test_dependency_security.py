"""Dependency and supply-chain regression tests (CLIP-092).

Verifies the 11_CI_CD_DEPLOYMENT.md lane-11 controls in-process so a
regression fails the normal test run, not only the nightly scan:

* declared runtime dependencies are pinned or bounded (no bare `*`),
* no dependency is pulled from a non-PyPI index (supply-chain surface),
* the CI workflow still runs the dependency audit, secret scan and SBOM
  lanes, and the container build does not run as root,
* no tracked file contains a private key or provider-looking secret.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
API_DIR = REPO_ROOT / "services" / "api"
PYPROJECT = API_DIR / "pyproject.toml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "baseline-ci.yml"

_BARE_PIN = re.compile(r"^\s*[A-Za-z0-9._-]+\s*$")
_NON_PYPI_INDEX = re.compile(
    r"(?:--index-url|--extra-index-url|index-url)\s*=\s*(?!https://pypi\.org)",
    re.IGNORECASE,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_PROVIDER_SECRET = re.compile(
    r"sk-[A-Za-z0-9]{20,}|sk-or-v1-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{36}"
    r"|github_pat_[A-Za-z0-9_]{22,}|AKIA[0-9A-Z]{16}",
)


def _dependencies_block(text: str) -> list[str]:
    """Return the entries of the [project] dependencies array."""
    match = re.search(r"^dependencies\s*=\s*\[(.*?)^\]", text, re.DOTALL | re.MULTILINE)
    if match is None:
        return []
    entries: list[str] = []
    for line in match.group(1).splitlines():
        stripped = line.strip().rstrip(",").strip().strip('"').strip("'")
        if stripped:
            entries.append(stripped)
    return entries


@pytest.fixture(scope="module")
def pyproject_text() -> str:
    if not PYPROJECT.exists():
        pytest.skip("pyproject.toml not present")
    return PYPROJECT.read_text(encoding="utf-8")


def test_runtime_dependencies_are_declared(pyproject_text: str) -> None:
    deps = _dependencies_block(pyproject_text)
    assert deps, "no runtime dependencies declared"


def test_dependencies_are_bounded_not_bare(pyproject_text: str) -> None:
    """Every dependency must carry a version constraint (no unpinned deps)."""
    unbounded = [d for d in _dependencies_block(pyproject_text) if _BARE_PIN.match(d)]
    assert unbounded == [], f"unpinned dependencies: {unbounded}"


def test_no_non_pypi_index_configured(pyproject_text: str) -> None:
    assert not _NON_PYPI_INDEX.search(pyproject_text), "non-PyPI package index configured"


def test_no_pip_config_override_in_repo() -> None:
    for candidate in ("pip.conf", "pip.ini", ".pip/pip.conf"):
        path = REPO_ROOT / candidate
        assert not path.exists(), f"repository pip config present: {candidate}"


def test_ci_runs_dependency_secret_and_sbom_lanes() -> None:
    assert WORKFLOW.exists(), "CI workflow missing"
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "dependency_audit.sh" in workflow, "dependency audit lane missing"
    assert "secret_scan.sh" in workflow, "secret scan lane missing"
    assert "cyclonedx" in workflow, "SBOM generation missing"
    assert "pip-audit" in workflow or "dependency_audit" in workflow


def test_ci_still_gates_format_lint_type_and_tests() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    for required in (
        "ruff format --check",
        "ruff check",
        "mypy services/api/clipos_api",
        "pytest",
    ):
        assert required in workflow, f"CI no longer runs: {required}"


def test_container_runs_as_non_root() -> None:
    dockerfile = API_DIR / "Dockerfile"
    assert dockerfile.exists(), "API Dockerfile missing"
    text = dockerfile.read_text(encoding="utf-8")
    assert re.search(r"^\s*USER\s+(?!root)\S+", text, re.MULTILINE), (
        "Dockerfile does not switch to a non-root USER"
    )


def test_no_docker_socket_mount_in_dev_compose() -> None:
    compose = REPO_ROOT / "docker-compose.dev.yml"
    if not compose.exists():
        pytest.skip("dev compose not present")
    text = compose.read_text(encoding="utf-8")
    assert "docker.sock" not in text, "Docker socket exposed to a service"


def test_tracked_files_have_no_private_keys_or_provider_secrets() -> None:
    """Independent of secret_scan.sh: scan tracked files in-process."""
    try:
        listing = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout
    except (subprocess.SubprocessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("git not available")

    offenders: list[str] = []
    for relative in listing.splitlines():
        path = REPO_ROOT / relative
        try:
            if path.stat().st_size > 2_000_000:
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, ValueError):
            continue
        if _PRIVATE_KEY.search(content) or _PROVIDER_SECRET.search(content):
            offenders.append(relative)
    assert offenders == [], f"secret-like content in tracked files: {offenders}"


def test_python_runtime_is_supported() -> None:
    assert sys.version_info >= (3, 12), f"unsupported runtime: {sys.version}"
