#!/usr/bin/env python3
"""Static architecture rules for ClipOS (stdlib only, read-only).

Checks the ADR set, the bounded-module enum, provider neutrality, the registry
contract, the CI job topology, and that BUILD_STATE.json parses.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_MODULES = {
    "identity",
    "organizations",
    "governance",
    "brands",
    "signals",
    "rights",
    "sources",
    "research",
    "content_planning",
    "media",
    "publishing",
    "analytics",
    "monetization",
    "finance",
    "experiments",
    "agents",
    "models",
    "tools_plugins",
    "workflows",
    "operations",
    "notifications",
}

PROVIDER_IDENTIFIER_PATTERN = re.compile(
    r"gpt-|claude-|glm-|deepseek|gemini|llama-\d|mistral|mixtral"
)


def check_adr_set() -> list[str]:
    adr_dir = ROOT / "docs" / "adr"
    problems: list[str] = []
    for name in ("README.md", "0000-template.md"):
        if not (adr_dir / name).is_file():
            problems.append(f"missing docs/adr/{name}")
    numbered = sorted(p.name for p in adr_dir.glob("0*.md") if p.name[0].isdigit())
    if len(numbered) < 4:
        problems.append(f"expected at least 4 numbered ADRs, found {len(numbered)}")
    return problems


def check_bounded_modules() -> list[str]:
    module_path = ROOT / "services" / "api" / "clipos_api" / "modules.py"
    spec = importlib.util.spec_from_file_location(
        "clipos_architecture_guard_modules", module_path
    )
    if spec is None or spec.loader is None:
        return ["cannot load services/api/clipos_api/modules.py"]
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    enum_cls = module.BoundedModule
    values = {member.value for member in enum_cls}
    missing = EXPECTED_MODULES - values
    if missing:
        return [f"BoundedModule is missing modules: {sorted(missing)}"]
    return []


def check_providers_neutral() -> list[str]:
    text = (ROOT / "services" / "api" / "clipos_api" / "providers.py").read_text(
        encoding="utf-8"
    )
    match = PROVIDER_IDENTIFIER_PATTERN.search(text)
    if match:
        return [
            f"providers.py contains a literal provider/model identifier: {match.group(0)!r}"
        ]
    return []


def check_registry_contract() -> list[str]:
    text = (ROOT / "services" / "api" / "clipos_api" / "registry.py").read_text(
        encoding="utf-8"
    )
    missing = [
        name for name in ("register", "get", "clear") if f"def {name}(" not in text
    ]
    if missing:
        return [f"registry.py is missing required functions: {missing}"]
    return []


def check_ci_topology() -> list[str]:
    text = (ROOT / ".github" / "workflows" / "baseline-ci.yml").read_text(
        encoding="utf-8"
    )
    problems: list[str] = []
    for job in ("validate", "test", "quality", "build"):
        if not re.search(rf"^  {job}:$", text, flags=re.MULTILINE):
            problems.append(f"baseline-ci.yml is missing job {job}")
    if not re.search(r"needs:\s*\[validate,\s*test,\s*quality,\s*build\]", text):
        problems.append(
            "baseline-ci.yml report job does not need all four jobs [validate, test, quality, build]"
        )
    return problems


def check_build_state_parses() -> list[str]:
    try:
        json.loads((ROOT / "BUILD_STATE.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"BUILD_STATE.json does not parse: {exc}"]
    return []


RULES: list[tuple[str, Callable[[], list[str]]]] = [
    ("adr-set", check_adr_set),
    ("bounded-modules", check_bounded_modules),
    ("provider-neutrality", check_providers_neutral),
    ("registry-contract", check_registry_contract),
    ("ci-topology", check_ci_topology),
    ("build-state", check_build_state_parses),
]


def run_guard(root: Path = ROOT) -> list[tuple[str, list[str]]]:
    """Run all rules; returns (rule, problems) pairs with problems only."""
    global ROOT
    original_root = ROOT
    ROOT = root
    try:
        results = []
        for name, rule in RULES:
            problems = rule()
            if problems:
                results.append((name, problems))
        return results
    finally:
        ROOT = original_root


def main() -> int:
    failures = run_guard()
    for name, rule in RULES:
        problems = dict(failures).get(name, [])
        status = "FAIL: " + "; ".join(problems) if problems else "OK"
        print(f"architecture_guard[{name}]: {status}")
    if failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
