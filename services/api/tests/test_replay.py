"""Offline workflow replay and deterministic-code guard (CI lane 8)."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from google.protobuf import json_format
from temporalio.api.history.v1 import History
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from clipos_api.reference_workflow import FrameworkSelfTestWorkflow

_FIXTURE = Path(__file__).parent / "fixtures" / "reference_workflow_history.json"


def _history() -> History:
    """Parse the orchestrator-exported event records into a protobuf History."""
    fixture: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert fixture["workflow_type"] == "FrameworkSelfTestWorkflow"
    return json_format.ParseDict({"events": fixture["events"]}, History())


def test_fixture_replays_without_nondeterminism() -> None:
    result = asyncio.run(
        Replayer(workflows=[FrameworkSelfTestWorkflow]).replay_workflow(
            WorkflowHistory("reference-workflow-fixture", _history().events)
        )
    )
    assert result.replay_failure is None


def _workflow_modules() -> Iterator[tuple[str, ast.Module]]:
    package = Path(__file__).resolve().parents[1] / "clipos_api"
    for name in ("reference_workflow.py", "workflow_framework.py"):
        yield name, ast.parse((package / name).read_text(encoding="utf-8"), filename=name)


def _forbidden_call(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    function = node.func
    if isinstance(function, ast.Attribute):
        name = ast.unparse(function)
    elif isinstance(function, ast.Name):
        name = function.id
    else:
        return None
    forbidden = {
        "time.sleep",
        "random.random",
        "random.randint",
        "random.choice",
        "asyncio.sleep",
        "datetime.now",
        "datetime.utcnow",
    }
    return name if name in forbidden else None


def test_workflow_classes_use_only_deterministic_time_and_scheduling() -> None:
    checked_classes: set[str] = set()
    for filename, tree in _workflow_modules():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorators = {ast.unparse(decorator) for decorator in node.decorator_list}
            # Guard workflow methods only. Activity bodies may legitimately use
            # wall-clock input, and service helpers are outside replay code.
            workflow_body = any(
                decorator in {"workflow.defn", "workflow.defn()", "workflow.run"}
                for decorator in decorators
            )
            if not workflow_body:
                continue
            checked_classes.add(f"{filename}:{node.name}")
            for descendant in ast.walk(node):
                if isinstance(descendant, (ast.Import, ast.ImportFrom)):
                    names = [
                        item.name
                        for item in (descendant.names if isinstance(descendant, ast.Import) else [])
                    ]
                    module = descendant.module if isinstance(descendant, ast.ImportFrom) else None
                    forbidden_modules = {"time", "random", "asyncio"}
                    assert not any(name in forbidden_modules for name in names)
                    assert module not in forbidden_modules
                forbidden = _forbidden_call(descendant)
                assert forbidden is None, f"nondeterministic call {forbidden} in {filename}"
    # Fail loudly if refactor names no longer expose the replay class to guard.
    assert any(name.endswith(":run") for name in checked_classes), checked_classes
