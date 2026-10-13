"""Tests for Jev System One decision tools (ad-hoc feature)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from clipos_api import audit
from clipos_api.jev_tools import (
    JevQuestion,
    JevRequest,
    MockJevAdapter,
    QuestionType,
    RemoteJevAdapter,
    get_jev_adapter,
)
from clipos_api.main import app


@pytest.fixture()
def audit_log(monkeypatch: pytest.MonkeyPatch) -> Path:
    path = Path("/tmp") / "clipos-jev-tools-audit.log"
    monkeypatch.setattr(audit, "AUDIT_PATH", path)
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_remote_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEV_BASE_URL", raising=False)
    monkeypatch.delenv("JEV_API_KEY", raising=False)


def _request(**overrides: object) -> JevRequest:
    payload: dict[str, object] = {
        "org_id": "org-1",
        "state": "Order 4411 arrived late and the box was crushed.",
        "questions": {
            "team": JevQuestion(
                type=QuestionType.CHOICE,
                instructions="Which team should handle this?",
                criteria={"shipping": "Delivery delays", "billing": "Charges"},
            ),
            "urgent": JevQuestion(
                type=QuestionType.NOUL,
                instructions="Does this need urgent human attention?",
            ),
        },
    }
    payload.update(overrides)
    return JevRequest(**payload)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def test_request_rejects_empty_questions() -> None:
    with pytest.raises(ValidationError):
        JevRequest(org_id="org-1", state="x", questions={})


def test_request_model_alias_default() -> None:
    request = _request()
    assert request.model_alias == "systemone-latest"


# ---------------------------------------------------------------------------
# Mock adapter
# ---------------------------------------------------------------------------


def test_mock_deterministic() -> None:
    adapter = MockJevAdapter()
    first = adapter.answer(_request())
    second = adapter.answer(_request())
    assert [a.probabilities for a in first.answers] == [a.probabilities for a in second.answers]
    assert [a.value for a in first.answers] == [a.value for a in second.answers]
    assert first.mode == "mock"


def test_mock_choice_probabilities_sum_to_one() -> None:
    adapter = MockJevAdapter()
    result = adapter.answer(_request())
    team = next(a for a in result.answers if a.question_key == "team")
    assert sum(team.probabilities.values()) == pytest.approx(1.0)
    assert team.value in team.probabilities


def test_mock_noul_yes_no() -> None:
    adapter = MockJevAdapter()
    result = adapter.answer(_request())
    urgent = next(a for a in result.answers if a.question_key == "urgent")
    assert set(urgent.probabilities) == {"yes", "no"}
    assert urgent.value in ("yes", "no")


def test_mock_choice_requires_criteria() -> None:
    adapter = MockJevAdapter()
    request = _request(
        questions={
            "bad": JevQuestion(type=QuestionType.CHOICE, instructions="Pick one"),
        }
    )
    with pytest.raises(ValueError, match="requires criteria"):
        adapter.answer(request)


def test_mock_score_levels() -> None:
    adapter = MockJevAdapter()
    request = _request(
        questions={
            "severity": JevQuestion(
                type=QuestionType.SCORE,
                instructions="How severe is this?",
                criteria={"low": "Minor", "high": "Severe"},
            ),
        }
    )
    result = adapter.answer(request)
    severity = result.answers[0]
    assert set(severity.probabilities) == {"low", "high"}
    assert severity.value in ("low", "high")


def test_mock_health() -> None:
    health = MockJevAdapter().health_check()
    assert health.ok is True
    assert "answer" in health.capabilities
    assert "mock" in (health.detail or "")


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------


def test_selection_defaults_to_mock() -> None:
    assert isinstance(get_jev_adapter(), MockJevAdapter)


def test_selection_remote_when_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEV_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("JEV_API_KEY", "env-key")
    adapter = get_jev_adapter()
    assert isinstance(adapter, RemoteJevAdapter)


# ---------------------------------------------------------------------------
# Remote adapter (MockTransport — no network)
# ---------------------------------------------------------------------------


def test_remote_posts_systemone_protocol() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "answers": {
                    "team": {
                        "probabilities": {"shipping": 0.8, "billing": 0.2},
                        "choice": "shipping",
                        "latency_ms": 12,
                    }
                },
                "latency_ms": 15,
            },
        )

    adapter = RemoteJevAdapter(
        "https://endpoint.example/",
        "test-key-123",
        transport=httpx.MockTransport(handler),
    )
    result = adapter.answer(
        _request(
            questions={
                "team": JevQuestion(
                    type=QuestionType.CHOICE,
                    instructions="Which team?",
                    criteria={"shipping": "Delivery", "billing": "Charges"},
                ),
            }
        )
    )
    assert len(captured) == 1
    request = captured[0]
    assert request.url.path == "/v1/systemone"
    assert request.headers["authorization"] == "Bearer test-key-123"
    body = json.loads(request.content)
    assert body["model"] == "systemone-latest"
    assert body["questions"]["team"]["type"] == "choice"
    assert result.mode == "remote"
    assert result.answers[0].value == "shipping"
    assert result.answers[0].latency_ms == 12


def test_remote_health_ok() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"models": []})

    adapter = RemoteJevAdapter(
        "https://endpoint.example",
        "k",
        transport=httpx.MockTransport(handler),
    )
    health = adapter.health_check()
    assert health.ok is True


def test_remote_health_unauthorized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    adapter = RemoteJevAdapter(
        "https://endpoint.example",
        "k",
        transport=httpx.MockTransport(handler),
    )
    health = adapter.health_check()
    assert health.ok is False
    assert "401" in (health.detail or "")


def test_remote_http_error_normalizes_to_valueerror() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = RemoteJevAdapter(
        "https://endpoint.example",
        "k",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="jev adapter request failed"):
        adapter.answer(_request())


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_routes_registered_on_app() -> None:
    paths = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert "/jev/answer" in paths
    assert "/jev/triage" in paths
    assert "/jev/health" in paths


@pytest.fixture()
def client(audit_log: Path):
    from fastapi.testclient import TestClient

    return TestClient(app)


def test_answer_roundtrip(client, audit_log: Path) -> None:
    resp = client.post(
        "/jev/answer",
        json={
            "org_id": "org-1",
            "state": "Order 4411 arrived late and the box was crushed.",
            "questions": {
                "team": {
                    "type": "choice",
                    "instructions": "Which team?",
                    "criteria": {"shipping": "Delivery", "billing": "Charges"},
                }
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "mock"
    assert len(body["answers"]) == 1
    assert "jev_tools.answered" in audit_log.read_text(encoding="utf-8")


def test_triage_roundtrip(client) -> None:
    resp = client.post(
        "/jev/triage",
        json={
            "org_id": "org-1",
            "state": "Customer was charged twice for the same order.",
            "categories": ["returns", "shipping", "billing"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    keys = {a["question_key"] for a in body["answers"]}
    assert keys == {"category", "urgent"}
    category = next(a for a in body["answers"] if a["question_key"] == "category")
    assert category["value"] in ("returns", "shipping", "billing")


def test_triage_without_urgency(client) -> None:
    resp = client.post(
        "/jev/triage",
        json={
            "org_id": "org-1",
            "state": "Routine status check.",
            "categories": ["a", "b"],
            "urgency": False,
        },
    )
    assert resp.status_code == 200, resp.text
    keys = {a["question_key"] for a in resp.json()["answers"]}
    assert keys == {"category"}


def test_answer_invalid_returns_422(client) -> None:
    resp = client.post("/jev/answer", json={"org_id": "org-1", "state": "x", "questions": {}})
    assert resp.status_code == 422


def test_health_route(client) -> None:
    resp = client.get("/jev/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_upstream_failure_returns_502(
    client,
    audit_log: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAdapter:
        def health_check(self):
            from clipos_api.jev_tools import JevHealth

            return JevHealth(ok=False, capabilities=[], detail="down")

        def answer(self, request: JevRequest):
            raise ValueError("jev adapter request failed: connection refused")

    from clipos_api import jev_tools

    monkeypatch.setattr(jev_tools, "get_jev_adapter", lambda: FailingAdapter())
    resp = client.post(
        "/jev/answer",
        json={
            "org_id": "org-1",
            "state": "x",
            "questions": {"q": {"type": "noul", "instructions": "ok?"}},
        },
    )
    assert resp.status_code == 502
    assert "request failed" in resp.json()["detail"]


def test_audit_never_contains_state_or_key(
    client,
    audit_log: Path,
) -> None:
    secret_state = "SENSITIVE-CUSTOMER-DETAIL-XYZZY"
    resp = client.post(
        "/jev/answer",
        json={
            "org_id": "org-1",
            "state": secret_state,
            "questions": {"q": {"type": "noul", "instructions": "ok?"}},
        },
    )
    assert resp.status_code == 200
    log = audit_log.read_text(encoding="utf-8")
    assert secret_state not in log
    assert "jev_tools.answered" in log
