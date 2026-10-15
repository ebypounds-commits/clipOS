"""Prompt-injection hardening and tool authorization (CLIP-091).

Implements the `07_SECURITY_GOVERNANCE_RIGHTS.md` prompt-injection
defenses as enforceable, testable code:

- **Detection** — `scan_untrusted_text` is a pure, deterministic scanner
  over retrieved/transcribed/web content. It labels spans that attempt
  to act as instructions rather than data: instruction overrides
  ("ignore previous instructions"), role/authority claims ("system:",
  "you are now"), tool/permission grants ("enable the shell tool",
  "grant yourself admin"), exfiltration requests ("send the api key
  to ..."), delimiter-escape attempts (fake `</system>` tags) and
  unicode lookalikes (zero-width and bidi characters). Each finding
  carries a kind, the matched span, and a severity.
- **Neutralization** — `neutralize_untrusted_text` returns the same
  text with every matched span replaced by an inert label
  (`[redacted-instruction:kind]`) and control characters stripped, so
  downstream prompts can only ever contain *data*. It also reports the
  counts per kind — no silent dropping.
- **Enforcement** — `enforce_untrusted_boundary` composes the scanner
  with a decision: content whose highest severity crosses the
  configured threshold is `QUARANTINED` (never passed on), otherwise it
  returns the neutralized text for safe use. The gate is fail-closed on
  scanner error (`SCANNER_ERROR`) and never throws to the caller.
- **Tool authorization** — `authorize_tool_from_content` makes the
  governance rule executable: **untrusted text can never grant a tool
  or capability**. A tool request whose justification reference comes
  from untrusted content is denied (`content_cannot_grant_tools`) even
  when the underlying policy engine would allow it; approval must come
  from a trusted actor reference.
- **Audit** — every enforcement decision persists to
  `injection_events` (migration 0068), append-only, idempotent per
  (org, event_id) with counts and the decision reason — never the raw
  untrusted text (which may itself be sensitive or hostile).

Routes (CLIP-033 pattern): POST /security/injection/scan (scan only),
POST /security/injection/enforce (scan + neutralize + decide, recorded),
GET /security/injection/events?org_id=. No LLM, no network.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

MAX_SCAN_CHARS = 20_000


class InjectionKind(str, Enum):
    """Classes of injection attempt found in untrusted content."""

    INSTRUCTION_OVERRIDE = "INSTRUCTION_OVERRIDE"
    ROLE_ASSUMPTION = "ROLE_ASSUMPTION"
    TOOL_GRANT_ATTEMPT = "TOOL_GRANT_ATTEMPT"
    EXFILTRATION_REQUEST = "EXFILTRATION_REQUEST"
    DELIMITER_ESCAPE = "DELIMITER_ESCAPE"
    HIDDEN_CHARACTER = "HIDDEN_CHARACTER"


class InjectionSeverity(str, Enum):
    """Severity of one finding."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_SEVERITY_RANK = {
    InjectionSeverity.LOW: 0,
    InjectionSeverity.MEDIUM: 1,
    InjectionSeverity.HIGH: 2,
    InjectionSeverity.CRITICAL: 3,
}


# Ordered patterns: (kind, severity, compiled regex).
_PATTERNS: list[tuple[InjectionKind, InjectionSeverity, re.Pattern[str]]] = [
    (
        InjectionKind.INSTRUCTION_OVERRIDE,
        InjectionSeverity.CRITICAL,
        re.compile(
            r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}?"
            r"\b(?:previous|prior|earlier|above|all)\b[^.\n]{0,20}?"
            r"\b(?:instruction|instructions|prompt|prompts|rule|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.ROLE_ASSUMPTION,
        InjectionSeverity.HIGH,
        re.compile(
            r"(?:^|\n)\s*(?:system|assistant|developer)\s*[:\-]"
            r"|\byou\s+are\s+now\b[^.\n]{0,60}"
            r"|\bnew\s+(?:system\s+)?(?:instructions?|prompt)\s*[:\-]",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.TOOL_GRANT_ATTEMPT,
        InjectionSeverity.CRITICAL,
        re.compile(
            r"\b(?:enable|activate|grant|give|allow|use)\b[^.\n]{0,30}?"
            r"\b(?:tool|tools|capabilit(?:y|ies)|permission|permissions|"
            r"shell|admin|sudo|root|access)\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.EXFILTRATION_REQUEST,
        InjectionSeverity.CRITICAL,
        re.compile(
            r"\b(?:send|post|upload|email|exfiltrate|leak|reveal|print)\b"
            r"[^.\n]{0,40}?\b(?:api[_\s-]?key|secret|token|password|credential|"
            r"env|environment|vault)\b",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.DELIMITER_ESCAPE,
        InjectionSeverity.HIGH,
        re.compile(
            r"</?\s*(?:system|instructions?|prompt|tool_call|function_call)\s*>",
            re.IGNORECASE,
        ),
    ),
    (
        InjectionKind.HIDDEN_CHARACTER,
        InjectionSeverity.HIGH,
        re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]"),
    ),
]


class InjectionFinding(BaseModel):
    """One detected span in untrusted content."""

    model_config = ConfigDict(frozen=True)

    kind: InjectionKind
    severity: InjectionSeverity
    match: str = Field(max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ScanReport(BaseModel):
    """Pure scanner output over one piece of untrusted text."""

    model_config = ConfigDict(frozen=True)

    findings: list[InjectionFinding] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    highest_severity: InjectionSeverity | None = None
    truncated: bool = False


class EnforcementDecision(str, Enum):
    """What the boundary decided to do with the content."""

    ALLOWED = "ALLOWED"
    NEUTRALIZED = "NEUTRALIZED"
    QUARANTINED = "QUARANTINED"
    SCANNER_ERROR = "SCANNER_ERROR"


class EnforcementResult(BaseModel):
    """Boundary outcome: safe text plus the decision and reason."""

    model_config = ConfigDict(frozen=True)

    decision: EnforcementDecision
    reason: str = Field(min_length=1, max_length=64)
    safe_text: str
    counts: dict[str, int] = Field(default_factory=dict)
    highest_severity: InjectionSeverity | None = None
    quarantined_findings: list[InjectionFinding] = Field(default_factory=list)


def scan_untrusted_text(text: str) -> ScanReport:
    """Pure: label spans that try to act as instructions."""
    if len(text) > MAX_SCAN_CHARS:
        text = text[:MAX_SCAN_CHARS]
        truncated = True
    else:
        truncated = False
    findings: list[InjectionFinding] = []
    for kind, severity, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            findings.append(
                InjectionFinding(
                    kind=kind,
                    severity=severity,
                    match=match.group(0)[:200],
                    start=match.start(),
                    end=match.end(),
                )
            )
    findings.sort(key=lambda f: (f.start, f.kind.value))
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind.value] = counts.get(finding.kind.value, 0) + 1
    highest = None
    if findings:
        highest = max(findings, key=lambda f: _SEVERITY_RANK[f.severity]).severity
    return ScanReport(
        findings=findings,
        counts=counts,
        highest_severity=highest,
        truncated=truncated,
    )


def _strip_control_characters(text: str) -> str:
    out: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in ("Cc", "Cf") and char not in ("\n", "\t"):
            continue
        out.append(char)
    return "".join(out)


def neutralize_untrusted_text(text: str, report: ScanReport) -> str:
    """Pure: replace every matched span with an inert label.

    Labels are applied right-to-left over the original text so every
    offset stays valid; zero-width/bidi characters are then dropped by
    the control-character strip, which runs last so it cannot shift the
    offsets used by the label edits.
    """
    result = text
    for finding in sorted(report.findings, key=lambda f: f.start, reverse=True):
        if finding.kind is InjectionKind.HIDDEN_CHARACTER:
            continue
        label = f"[redacted-instruction:{finding.kind.value.lower()}]"
        result = result[: finding.start] + label + result[finding.end :]
    return _strip_control_characters(result)


def enforce_untrusted_boundary(
    text: str, quarantine_at: InjectionSeverity = InjectionSeverity.CRITICAL
) -> EnforcementResult:
    """Pure-ish boundary: scan, neutralize, or quarantine. Never raises."""
    try:
        report = scan_untrusted_text(text)
    except Exception as exc:  # noqa: BLE001 - boundary must fail closed
        return EnforcementResult(
            decision=EnforcementDecision.SCANNER_ERROR,
            reason=f"scanner_error:{type(exc).__name__}"[:64],
            safe_text="",
        )
    if report.highest_severity is not None and (
        _SEVERITY_RANK[report.highest_severity] >= _SEVERITY_RANK[quarantine_at]
    ):
        blocked = [f for f in report.findings if f.severity == report.highest_severity]
        return EnforcementResult(
            decision=EnforcementDecision.QUARANTINED,
            reason=f"severity_{report.highest_severity.value.lower()}",
            safe_text="",
            counts=report.counts,
            highest_severity=report.highest_severity,
            quarantined_findings=blocked[:20],
        )
    safe = neutralize_untrusted_text(text, report)
    if report.findings:
        return EnforcementResult(
            decision=EnforcementDecision.NEUTRALIZED,
            reason="spans_neutralized",
            safe_text=safe,
            counts=report.counts,
            highest_severity=report.highest_severity,
        )
    return EnforcementResult(
        decision=EnforcementDecision.ALLOWED,
        reason="no_injection_detected",
        safe_text=safe,
        counts={},
        highest_severity=None,
    )


class ToolAuthorizationRequest(BaseModel):
    """One tool request whose justification comes from some content."""

    model_config = ConfigDict(frozen=True)

    tool_id: str = Field(min_length=1, max_length=255)
    requested_by_ref: str = Field(min_length=1, max_length=512)
    justification_source: str = Field(min_length=1, max_length=32)


class ToolAuthorization(BaseModel):
    """Outcome of the content-cannot-grant-tools rule."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=64)


TRUSTED_SOURCES = frozenset({"OPERATOR", "POLICY", "SYSTEM"})


def authorize_tool_from_content(request: ToolAuthorizationRequest) -> ToolAuthorization:
    """Pure: untrusted content can never authorize a tool.

    ``justification_source`` names where the request came from; anything
    outside the trusted set (OPERATOR/POLICY/SYSTEM) is denied, so a
    page, transcript or web result cannot grant itself capability.
    """
    if request.justification_source.upper() not in TRUSTED_SOURCES:
        return ToolAuthorization(allowed=False, reason="content_cannot_grant_tools")
    return ToolAuthorization(allowed=True, reason="trusted_actor")


class InjectionEventRecord(Base):
    """SQL row for one enforcement decision (append-only)."""

    __tablename__ = "injection_events"
    __table_args__ = (
        sa.Index("uq_injection_events_identity", "org_id", "event_id", unique=True),
        sa.Index("ix_injection_events_org_decision", "org_id", "decision"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    decision: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    highest_severity: Mapped[str | None] = mapped_column(sa.String(16), nullable=True)
    counts: Mapped[dict] = mapped_column(sa.JSON, nullable=False, default=dict)
    content_chars: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class InjectionEvent(BaseModel):
    """One recorded enforcement decision (no raw content stored)."""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    source_ref: str = Field(min_length=1, max_length=512)
    decision: EnforcementDecision
    reason: str = Field(min_length=1, max_length=64)
    highest_severity: InjectionSeverity | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    content_chars: int = Field(ge=0)
    created_at: datetime | None = None


class ScanRequest(BaseModel):
    """Request body for a scan-only call."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=MAX_SCAN_CHARS)


class EnforceRequest(BaseModel):
    """Request body for one enforced boundary call."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    source_ref: str = Field(min_length=1, max_length=512)
    text: str = Field(min_length=1, max_length=MAX_SCAN_CHARS)
    quarantine_at: InjectionSeverity = InjectionSeverity.CRITICAL


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _event_from_row(row: InjectionEventRecord) -> InjectionEvent:
    return InjectionEvent(
        event_id=row.event_id,
        org_id=row.org_id,
        source_ref=row.source_ref,
        decision=EnforcementDecision(row.decision),
        reason=row.reason,
        highest_severity=(
            InjectionSeverity(row.highest_severity) if row.highest_severity else None
        ),
        counts={str(k): int(v) for k, v in (row.counts or {}).items()},
        content_chars=row.content_chars,
        created_at=_aware(row.created_at),
    )


class InjectionStore:
    """Typed persistence for injection enforcement events."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> InjectionStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def enforce(self, request: EnforceRequest) -> EnforcementResult:
        """Run the boundary and record a content-free event (conflict 409)."""
        result = enforce_untrusted_boundary(request.text, request.quarantine_at)
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(InjectionEventRecord).where(
                    InjectionEventRecord.org_id == request.org_id,
                    InjectionEventRecord.event_id == request.event_id,
                )
            ).first()
            if existing is not None:
                stored = _event_from_row(existing)
                if (
                    stored.decision != result.decision
                    or stored.source_ref != request.source_ref
                    or stored.counts != result.counts
                ):
                    raise ValueError(
                        f"injection event already recorded with different fields: "
                        f"{request.event_id}"
                    )
                return EnforcementResult(
                    decision=stored.decision,
                    reason=stored.reason,
                    safe_text=(
                        result.safe_text
                        if stored.decision is not EnforcementDecision.QUARANTINED
                        else ""
                    ),
                    counts=stored.counts,
                    highest_severity=stored.highest_severity,
                )
            row = InjectionEventRecord(
                event_id=request.event_id,
                org_id=request.org_id,
                source_ref=request.source_ref,
                decision=result.decision.value,
                reason=result.reason,
                highest_severity=(
                    result.highest_severity.value if result.highest_severity else None
                ),
                counts=dict(result.counts),
                content_chars=len(request.text),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "injection_events.recorded",
                "injection_events",
                f"injection_event:{request.event_id}",
                {
                    "org_id": request.org_id,
                    "source_ref": request.source_ref,
                    "decision": result.decision.value,
                    "reason": result.reason,
                    "counts": dict(result.counts),
                },
            )
            return result

    def list_events(
        self, org_id: str, decision: EnforcementDecision | None = None
    ) -> list[InjectionEvent]:
        with self._sessions() as session:
            stmt = sa.select(InjectionEventRecord).where(InjectionEventRecord.org_id == org_id)
            if decision is not None:
                stmt = stmt.where(InjectionEventRecord.decision == decision.value)
            rows = session.scalars(
                stmt.order_by(InjectionEventRecord.created_at, InjectionEventRecord.id)
            ).all()
            return [_event_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["security"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> InjectionStore:
    return InjectionStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post("/security/injection/scan", response_model=ScanReport, name="scan_injection")
def scan_injection_route(request: ScanRequest) -> ScanReport:
    """Scan untrusted text without persisting anything."""
    return scan_untrusted_text(request.text)


@router.post(
    "/security/injection/enforce",
    response_model=EnforcementResult,
    name="enforce_injection_boundary",
)
def enforce_injection_boundary_route(
    request: EnforceRequest,
    store: Annotated[InjectionStore, Depends(_store)],
) -> EnforcementResult:
    """Scan, neutralize or quarantine, and record the decision."""
    try:
        return store.enforce(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/security/injection/events",
    response_model=list[InjectionEvent],
    name="list_injection_events",
)
def list_injection_events_route(
    store: Annotated[InjectionStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    decision: Annotated[EnforcementDecision | None, Query()] = None,
) -> list[InjectionEvent]:
    """List recorded enforcement decisions for one org."""
    return store.list_events(org_id, decision)


@router.post(
    "/security/tool-authorization",
    response_model=ToolAuthorization,
    name="authorize_tool_from_content",
)
def authorize_tool_from_content_route(
    request: ToolAuthorizationRequest,
) -> ToolAuthorization:
    """Untrusted content can never authorize a tool (fail-closed)."""
    return authorize_tool_from_content(request)
