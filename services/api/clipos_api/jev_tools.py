"""Jev System One decision tools (ad-hoc feature).

Typed decision questions over a text state, behind a provider-neutral
adapter (TypeSafe's System One protocol — choice / noul / score, no
text generation):

- **Adapter contract** — ``JevAdapter`` Protocol (health_check, answer).
  ``MockJevAdapter`` is the deterministic CI stand-in: per-question
  probabilities derive from ``sha1(state|question|criteria)`` so the
  same request always yields the same answers — no network, no key.
  ``RemoteJevAdapter`` speaks ``POST {JEV_BASE_URL}/v1/systemone`` with
  ``authorization: Bearer {JEV_API_KEY}``; both values come from env
  only (secret reference discipline — the key is never persisted,
  logged, or audited). Selection: both env vars set -> remote, else
  mock (same opt-in shape as the local LLM adapter).
- **Model identity** — callers pass a registry-style ``model_alias``
  (default ``systemone-latest``); the adapter forwards it as the
  protocol ``model`` field. No model or vendor identifier is hard-coded.
- **Tools** — ``POST /jev/answer`` runs arbitrary typed questions;
  ``POST /jev/triage`` is the two-question convenience (caller-supplied
  category choice + urgency noul); ``GET /jev/health`` reports adapter
  capabilities. Upstream failures normalize to 502 (bounded detail, no
  key material).

Audits: ``jev_tools.answered`` (org, tool, question count, mode —
never the state text or the key). No table, no migration.
"""

from __future__ import annotations

import os
from enum import Enum
from hashlib import sha1
from typing import Annotated, Literal, Protocol
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from clipos_api import audit


class QuestionType(str, Enum):
    """System One question kinds (typed answers, no generation)."""

    CHOICE = "choice"
    NOUL = "noul"
    SCORE = "score"


class JevQuestion(BaseModel):
    """One typed question; criteria maps option/level -> description."""

    model_config = ConfigDict(frozen=True)

    type: QuestionType
    instructions: str = Field(min_length=1, max_length=500)
    criteria: dict[str, str] = Field(default_factory=dict)


class JevRequest(BaseModel):
    """One batch of typed questions over a text state."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=8000)
    model_alias: str = Field(default="systemone-latest", min_length=1, max_length=128)
    questions: dict[str, JevQuestion] = Field(min_length=1, max_length=8)


class JevAnswer(BaseModel):
    """Normalized answer for one question key."""

    model_config = ConfigDict(frozen=True)

    question_key: str = Field(min_length=1, max_length=64)
    probabilities: dict[str, float] = Field(default_factory=dict)
    value: str | None = Field(default=None, max_length=64)
    latency_ms: float | None = Field(default=None, ge=0)
    adapter_kind: Literal["mock", "real"] = "real"


class JevResult(BaseModel):
    """Normalized batch result."""

    model_config = ConfigDict(frozen=True)

    answers: list[JevAnswer] = Field(min_length=1)
    model_alias: str = Field(min_length=1, max_length=128)
    mode: str = Field(min_length=1, max_length=16)
    latency_ms: float | None = Field(default=None, ge=0)


class JevHealth(BaseModel):
    """Normalized health probe result."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = Field(default=None, max_length=255)


class JevAdapter(Protocol):
    """Provider-neutral decision backend."""

    def health_check(self) -> JevHealth: ...

    def answer(self, request: JevRequest) -> JevResult: ...


def _seed(value: str) -> int:
    return int(sha1(value.encode()).hexdigest(), 16)


def _weights(basis: str, labels: list[str]) -> dict[str, float]:
    """Deterministic positive weights normalized to sum 1.0."""
    raw = [_seed(f"{basis}|{label}") % 10_000 + 1 for label in labels]
    total = float(sum(raw))
    return {label: raw[i] / total for i, label in enumerate(labels)}


class MockJevAdapter:
    """Deterministic stand-in: probabilities derive from sha1 of the input."""

    def health_check(self) -> JevHealth:
        return JevHealth(
            ok=True,
            capabilities=["answer", "choice", "noul", "score", "triage"],
            detail="mock Jev adapter — set JEV_BASE_URL and JEV_API_KEY for remote",
        )

    def answer(self, request: JevRequest) -> JevResult:
        answers: list[JevAnswer] = []
        for key, question in request.questions.items():
            if question.type is QuestionType.NOUL:
                probability = _seed(f"{request.state}|{key}") % 10_000 / 10_000.0
                probabilities = {"yes": probability, "no": 1.0 - probability}
                value = "yes" if probability >= 0.5 else "no"
            else:
                if not question.criteria:
                    raise ValueError(f"{question.type.value} question requires criteria: {key}")
                labels = list(question.criteria.keys())
                probabilities = _weights(f"{request.state}|{key}", labels)
                value = max(probabilities, key=lambda label: probabilities[label])
            answers.append(
                JevAnswer(
                    question_key=key,
                    probabilities=probabilities,
                    value=value,
                    latency_ms=float(_seed(f"latency|{key}") % 4 + 1),
                )
            )
        return JevResult(
            answers=answers,
            model_alias=request.model_alias,
            mode="mock",
            latency_ms=max((a.latency_ms or 0.0) for a in answers),
        )


class RemoteJevAdapter:
    """HTTP adapter for a System One endpoint (env-provided URL + key)."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            transport=self._transport,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self._api_key}",
            "content-type": "application/json",
        }

    def health_check(self) -> JevHealth:
        try:
            with self._client() as client:
                response = client.get("/v1/models", headers=self._headers())
        except httpx.HTTPError as exc:
            return JevHealth(ok=False, capabilities=[], detail=str(exc)[:200])
        if response.status_code == 200:
            return JevHealth(
                ok=True,
                capabilities=["answer", "choice", "noul", "score"],
                detail="remote System One endpoint healthy",
            )
        return JevHealth(
            ok=False,
            capabilities=[],
            detail=f"remote endpoint returned status {response.status_code}",
        )

    def answer(self, request: JevRequest) -> JevResult:
        body: dict[str, object] = {
            "state": request.state,
            "model": request.model_alias,
            "questions": {
                key: _question_to_payload(question) for key, question in request.questions.items()
            },
        }
        try:
            with self._client() as client:
                response = client.post("/v1/systemone", headers=self._headers(), json=body)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(f"jev adapter request failed: {str(exc)[:300]}") from exc
        return _parse_answer_payload(data, request.model_alias)


def _question_to_payload(question: JevQuestion) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": question.type.value,
        "instructions": question.instructions,
    }
    if question.criteria:
        payload["criteria"] = dict(question.criteria)
    return payload


def _parse_answer_payload(data: object, model_alias: str) -> JevResult:
    """Parse a System One response (tolerates top-level or answers-wrapped)."""
    if type(data) is not dict:
        raise ValueError("jev adapter returned invalid payload")
    latency_raw = data.get("latency_ms")
    source = data.get("answers")
    if not isinstance(source, dict):
        source = {k: v for k, v in data.items() if isinstance(v, dict)}
    answers: list[JevAnswer] = []
    for key, payload in source.items():
        if not isinstance(payload, dict):
            continue
        probabilities = payload.get("probabilities") or {}
        if not isinstance(probabilities, dict):
            probabilities = {}
        value = payload.get("choice")
        if value is None:
            value = payload.get("noul")
        if value is None:
            value = payload.get("score")
        question_latency = payload.get("latency_ms")
        answers.append(
            JevAnswer(
                question_key=str(key)[:64],
                probabilities={str(k): float(v) for k, v in probabilities.items()},
                value=str(value)[:64] if value is not None else None,
                latency_ms=float(question_latency)
                if isinstance(question_latency, (int, float))
                else None,
            )
        )
    if not answers:
        raise ValueError("jev adapter returned no answers")
    return JevResult(
        answers=answers,
        model_alias=model_alias,
        mode="remote",
        latency_ms=float(latency_raw) if isinstance(latency_raw, (int, float)) else None,
    )


def get_jev_adapter() -> JevAdapter:
    """Remote when JEV_BASE_URL and JEV_API_KEY are both set, else mock."""
    base_url = os.environ.get("JEV_BASE_URL", "")
    api_key = os.environ.get("JEV_API_KEY", "")
    if base_url and api_key:
        timeout = float(os.environ.get("JEV_TIMEOUT_SECONDS", "30"))
        return RemoteJevAdapter(base_url, api_key, timeout)
    return MockJevAdapter()


class TriageRequest(BaseModel):
    """Convenience tool: category + urgency over one state."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    state: str = Field(min_length=1, max_length=8000)
    categories: list[str] = Field(min_length=2, max_length=16)
    urgency: bool = True
    model_alias: str = Field(default="systemone-latest", min_length=1, max_length=128)


def _run(
    adapter: JevAdapter,
    request: JevRequest,
    tool: str,
) -> JevResult:
    try:
        result = adapter.answer(request)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)[:512]) from exc
    audit.record(
        "jev_tools.answered",
        "jev_tools",
        f"jev:{str(uuid4())[:12]}",
        {
            "org_id": request.org_id,
            "tool": tool,
            "question_count": len(request.questions),
            "model_alias": request.model_alias,
            "mode": result.mode,
        },
    )
    return result


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["jev-tools"])


def _adapter() -> JevAdapter:
    return get_jev_adapter()


@router.post("/jev/answer", response_model=JevResult, name="jev_answer")
def jev_answer_route(
    request: JevRequest,
    adapter: Annotated[JevAdapter, Depends(_adapter)],
) -> JevResult:
    """Run typed decision questions over a text state."""
    return _run(adapter, request, tool="answer")


@router.post("/jev/triage", response_model=JevResult, name="jev_triage")
def jev_triage_route(
    request: TriageRequest,
    adapter: Annotated[JevAdapter, Depends(_adapter)],
) -> JevResult:
    """Classify a state into caller-supplied categories (+ optional urgency)."""
    criteria = {category: category for category in request.categories}
    questions: dict[str, JevQuestion] = {
        "category": JevQuestion(
            type=QuestionType.CHOICE,
            instructions="Which category best describes this state?",
            criteria=criteria,
        )
    }
    if request.urgency:
        questions["urgent"] = JevQuestion(
            type=QuestionType.NOUL,
            instructions="Does this need urgent human attention?",
        )
    jev_request = JevRequest(
        org_id=request.org_id,
        state=request.state,
        model_alias=request.model_alias,
        questions=questions,
    )
    return _run(adapter, jev_request, tool="triage")


@router.get("/jev/health", response_model=JevHealth, name="jev_health")
def jev_health_route(
    adapter: Annotated[JevAdapter, Depends(_adapter)],
) -> JevHealth:
    """Report decision-adapter health and capabilities."""
    return adapter.health_check()
