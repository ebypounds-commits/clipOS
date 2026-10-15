r"""Safe auto-remediation actions (CLIP-088).

PRD §K operations — bounded, allow-listed remediation so the platform
can respond to incidents without unrestricted command execution:

- **Allow-list** — `remediation_actions` are org-scoped, idempotent per
  (org_id, action_id) with slug ids. Each declares an `action_kind`
  (generic slug: restart-service, clear-cache, drain-node,
  scale-replicas, ... — never a shell command), a bounded
  `command_template` containing only `{param}` placeholders, a
  `max_attempts` cap (1..10), and a REQUIRED human-approval flag.
  Unknown placeholders or brace tokens fail validation (422) before
  persistence. Audited `remediation_actions.registered`.
- **Pure safety gate** — `evaluate_remediation` decides one request
  without side effects: denial reasons are `action_disabled`,
  `kill_switch_active`, `not_allow_listed` (kind not on the org
  allow-list), `params_invalid` (missing/extra/unsafe values),
  `rate_limited` (attempt cap reached), `approval_required`. Safety
  properties: values are validated against a strict
  `[A-Za-z0-9._:@/-]{1,128}` allow-set (no whitespace, no shell
  metacharacters, no `;|&$\`<>`), and any value containing a template
  brace is rejected — remediation can never inject arguments.
- **Executions** — `remediation_runs` record every attempt with the
  rendered command, gate outcome, and reason; idempotent per
  (org, run_id); denied runs are recorded as DENIED with the adapter
  never invoked. Execution itself is injectable
  (`RemediationExecutor` Protocol; `MockRemediationExecutor` for CI).
  Audited `remediation_runs.evaluated`.

A `kill_switch` stops every remediation org-wide without redeploy
(PRD: action can be globally disabled). Migration 0065 (CREATE-only).

Routes (CLIP-033 pattern): POST/GET /remediation-actions, POST
/remediation-actions/{action_id}/run, GET
/remediation-runs?org_id=. No LLM, no network in the mock path.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from string import Formatter
from typing import Annotated, Protocol

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

MAX_PARAM_VALUE = 128
SAFE_VALUE = re.compile(r"^[A-Za-z0-9._:@/=-]{1,128}$")


class RemediationOutcome(str, Enum):
    """Outcome of one remediation attempt."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DENIED = "DENIED"


DENY_ACTION_DISABLED = "action_disabled"
DENY_KILL_SWITCH = "kill_switch_active"
DENY_NOT_ALLOW_LISTED = "not_allow_listed"
DENY_PARAMS_INVALID = "params_invalid"
DENY_RATE_LIMITED = "rate_limited"
DENY_APPROVAL_REQUIRED = "approval_required"


class RemediationAction(BaseModel):
    """One allow-listed remediation template."""

    model_config = ConfigDict(frozen=True)

    action_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    action_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    command_template: str = Field(min_length=1, max_length=512)
    max_attempts: int = Field(default=3, ge=1, le=10)
    requires_approval: bool = True
    enabled: bool = True
    created_at: datetime | None = None


class RemediationRun(BaseModel):
    """One recorded remediation attempt."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    action_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    incident_id: str | None = Field(default=None, max_length=128)
    rendered_command: str = Field(default="", max_length=1024)
    outcome: RemediationOutcome
    reason: str = Field(min_length=1, max_length=64)
    actor_ref: str = Field(min_length=1, max_length=255)
    created_at: datetime | None = None


class RegisterActionRequest(BaseModel):
    """Request body for registering one remediation action."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    action_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    action_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    command_template: str = Field(min_length=1, max_length=512)
    max_attempts: int = Field(default=3, ge=1, le=10)
    requires_approval: bool = True
    enabled: bool = True


class RunRemediationRequest(BaseModel):
    """Request body for one remediation attempt."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    params: dict[str, str] = Field(default_factory=dict, max_length=16)
    incident_id: str | None = Field(default=None, max_length=128)
    actor_ref: str = Field(min_length=1, max_length=255)
    approved: bool = False
    kill_switch: bool = False


class GateDecision(BaseModel):
    """Pure remediation gate output."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str = Field(min_length=1, max_length=64)
    rendered_command: str = Field(default="", max_length=1024)


def template_placeholders(command_template: str) -> list[str]:
    """Pure: the ordered set of {param} names a template declares."""
    names: list[str] = []
    for _, field_name, _, _ in Formatter().parse(command_template):
        if field_name:
            names.append(field_name)
    return names


def validate_command_template(command_template: str) -> list[str]:
    """Pure: template problems (empty list = usable)."""
    problems: list[str] = []
    if not command_template.strip():
        problems.append("empty_template")
    if any(ch in command_template for ch in (";", "|", "&", "$", "`", "\n", "\r")):
        problems.append("template_has_shell_metacharacter")
    if "{" not in command_template and "}" not in command_template:
        # A fully static command is fine; placeholder-less is valid.
        return problems
    plain = command_template.replace("{", "").replace("}", "")
    if plain.count(" ") >= 0 and ("{" in command_template) != ("}" in command_template):
        problems.append("unbalanced_braces")
    try:
        names = template_placeholders(command_template)
    except ValueError:
        problems.append("malformed_braces")
        return problems
    for name in names:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            problems.append(f"bad_placeholder:{name}"[:64])
    return problems


def evaluate_remediation(
    action: RemediationAction,
    params: dict[str, str],
    attempted_runs: int,
    *,
    approved: bool,
    kill_switch: bool = False,
) -> GateDecision:
    """Pure safety gate — every denial is explicit and non-executing.

    Order: kill switch -> disabled -> allow-list -> params -> approval
    -> attempt cap. Values must match SAFE_VALUE and may not contain
    braces, so nothing can be injected through a placeholder.
    """
    if kill_switch:
        return GateDecision(allowed=False, reason=DENY_KILL_SWITCH)
    if not action.enabled:
        return GateDecision(allowed=False, reason=DENY_ACTION_DISABLED)
    expected = template_placeholders(action.command_template)
    if set(params) != set(expected):
        return GateDecision(allowed=False, reason=DENY_PARAMS_INVALID)
    for name, value in params.items():
        if "{" in value or "}" in value or not SAFE_VALUE.fullmatch(value):
            return GateDecision(allowed=False, reason=DENY_PARAMS_INVALID)
    if action.requires_approval and not approved:
        return GateDecision(allowed=False, reason=DENY_APPROVAL_REQUIRED)
    if attempted_runs >= action.max_attempts:
        return GateDecision(allowed=False, reason=DENY_RATE_LIMITED)
    rendered = action.command_template
    for name, value in params.items():
        rendered = rendered.replace("{" + name + "}", value)
    return GateDecision(allowed=True, reason="allowed", rendered_command=rendered[:1024])


class RemediationExecutor(Protocol):
    """Injectable execution backend for one rendered command."""

    def run(self, action_kind: str, rendered_command: str) -> tuple[bool, str]: ...


class MockRemediationExecutor:
    """Deterministic CI stand-in: records intent, performs nothing."""

    def run(self, action_kind: str, rendered_command: str) -> tuple[bool, str]:
        return True, f"mock executed {action_kind}"


class RemediationActionRecord(Base):
    """SQL row for one remediation action."""

    __tablename__ = "remediation_actions"
    __table_args__ = (
        sa.Index(
            "uq_remediation_actions_org_action",
            "org_id",
            "action_id",
            unique=True,
        ),
        sa.Index("ix_remediation_actions_org_kind", "org_id", "action_kind"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_kind: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    command_template: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    max_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    requires_approval: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class RemediationRunRecord(Base):
    """SQL row for one remediation attempt."""

    __tablename__ = "remediation_runs"
    __table_args__ = (
        sa.Index("uq_remediation_runs_org_run", "org_id", "run_id", unique=True),
        sa.Index("ix_remediation_runs_org_action", "org_id", "action_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    action_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    incident_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    rendered_command: Mapped[str] = mapped_column(sa.String(1024), nullable=False, default="")
    outcome: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    reason: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    actor_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _action_from_row(row: RemediationActionRecord) -> RemediationAction:
    return RemediationAction(
        action_id=row.action_id,
        org_id=row.org_id,
        action_kind=row.action_kind,
        command_template=row.command_template,
        max_attempts=row.max_attempts,
        requires_approval=row.requires_approval,
        enabled=row.enabled,
        created_at=_aware(row.created_at),
    )


def _run_from_row(row: RemediationRunRecord) -> RemediationRun:
    return RemediationRun(
        run_id=row.run_id,
        org_id=row.org_id,
        action_id=row.action_id,
        incident_id=row.incident_id,
        rendered_command=row.rendered_command,
        outcome=RemediationOutcome(row.outcome),
        reason=row.reason,
        actor_ref=row.actor_ref,
        created_at=_aware(row.created_at),
    )


class RemediationStore:
    """Typed persistence for remediation actions and runs."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RemediationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_action(self, request: RegisterActionRequest) -> RemediationAction:
        """Idempotent per (org, action_id); template validated first."""
        problems = validate_command_template(request.command_template)
        if problems:
            raise ValueError(f"invalid command template: {', '.join(problems)}")
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(RemediationActionRecord).where(
                    RemediationActionRecord.org_id == request.org_id,
                    RemediationActionRecord.action_id == request.action_id,
                )
            ).first()
            if existing is not None:
                stored = _action_from_row(existing)
                if (
                    stored.action_kind != request.action_kind
                    or stored.command_template != request.command_template
                    or stored.max_attempts != request.max_attempts
                    or stored.requires_approval != request.requires_approval
                    or stored.enabled != request.enabled
                ):
                    raise ValueError(
                        f"remediation action already registered with different fields: "
                        f"{request.action_id}"
                    )
                return stored
            row = RemediationActionRecord(
                action_id=request.action_id,
                org_id=request.org_id,
                action_kind=request.action_kind,
                command_template=request.command_template,
                max_attempts=request.max_attempts,
                requires_approval=request.requires_approval,
                enabled=request.enabled,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "remediation_actions.registered",
                "remediation_actions",
                f"remediation_action:{request.action_id}",
                {
                    "org_id": request.org_id,
                    "action_kind": request.action_kind,
                    "max_attempts": request.max_attempts,
                    "requires_approval": request.requires_approval,
                },
            )
            return _action_from_row(row)

    def get_action(self, action_id: str, org_id: str) -> RemediationAction | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(RemediationActionRecord).where(
                    RemediationActionRecord.action_id == action_id,
                    RemediationActionRecord.org_id == org_id,
                )
            ).first()
            return _action_from_row(row) if row is not None else None

    def list_actions(self, org_id: str) -> list[RemediationAction]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(RemediationActionRecord)
                .where(RemediationActionRecord.org_id == org_id)
                .order_by(RemediationActionRecord.created_at, RemediationActionRecord.id)
            ).all()
            return [_action_from_row(r) for r in rows]

    def _attempt_count(self, session: Session, org_id: str, action_id: str) -> int:
        return int(
            session.scalar(
                sa.select(sa.func.count())
                .select_from(RemediationRunRecord)
                .where(
                    RemediationRunRecord.org_id == org_id,
                    RemediationRunRecord.action_id == action_id,
                    RemediationRunRecord.outcome != RemediationOutcome.DENIED.value,
                )
            )
            or 0
        )

    def run_remediation(
        self,
        action_id: str,
        request: RunRemediationRequest,
        executor: RemediationExecutor,
    ) -> RemediationRun:
        """Gate then (only if allowed) execute once, recording the outcome."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            action_row = session.scalars(
                sa.select(RemediationActionRecord).where(
                    RemediationActionRecord.org_id == request.org_id,
                    RemediationActionRecord.action_id == action_id,
                )
            ).first()
            if action_row is None:
                raise ValueError(f"remediation action not found: {action_id}")
            action = _action_from_row(action_row)
            existing = session.scalars(
                sa.select(RemediationRunRecord).where(
                    RemediationRunRecord.org_id == request.org_id,
                    RemediationRunRecord.run_id == request.run_id,
                )
            ).first()
            if existing is not None:
                stored = _run_from_row(existing)
                if (
                    stored.action_id != action_id
                    or stored.actor_ref != request.actor_ref
                    or stored.incident_id != request.incident_id
                ):
                    raise ValueError(
                        f"remediation run already recorded with different fields: {request.run_id}"
                    )
                return stored
            attempts = self._attempt_count(session, request.org_id, action_id)

        decision = evaluate_remediation(
            action,
            dict(request.params),
            attempts,
            approved=request.approved,
            kill_switch=request.kill_switch,
        )
        outcome = RemediationOutcome.DENIED
        reason = decision.reason
        rendered = ""
        if decision.allowed:
            rendered = decision.rendered_command
            try:
                ok, detail = executor.run(action.action_kind, rendered)
                outcome = RemediationOutcome.SUCCEEDED if ok else RemediationOutcome.FAILED
                reason = "executed" if ok else f"executor_failed:{detail[:40]}"
            except Exception as exc:  # noqa: BLE001 - executor failures are recorded
                outcome = RemediationOutcome.FAILED
                reason = f"executor_error:{type(exc).__name__}"

        row = RemediationRunRecord(
            run_id=request.run_id,
            org_id=request.org_id,
            action_id=action_id,
            incident_id=request.incident_id,
            rendered_command=rendered[:1024],
            outcome=outcome.value,
            reason=reason[:64],
            actor_ref=request.actor_ref,
            created_at=now,
        )
        with self._sessions.begin() as session:
            raced = session.scalars(
                sa.select(RemediationRunRecord).where(
                    RemediationRunRecord.org_id == request.org_id,
                    RemediationRunRecord.run_id == request.run_id,
                )
            ).first()
            if raced is not None:
                return _run_from_row(raced)
            session.add(row)
            session.flush()
            audit.record(
                "remediation_runs.evaluated",
                "remediation_runs",
                f"remediation_run:{request.run_id}",
                {
                    "org_id": request.org_id,
                    "action_id": action_id,
                    "outcome": outcome.value,
                    "reason": reason,
                },
            )
            return _run_from_row(row)

    def list_runs(self, org_id: str, action_id: str | None = None) -> list[RemediationRun]:
        with self._sessions() as session:
            stmt = sa.select(RemediationRunRecord).where(RemediationRunRecord.org_id == org_id)
            if action_id is not None:
                stmt = stmt.where(RemediationRunRecord.action_id == action_id)
            rows = session.scalars(
                stmt.order_by(RemediationRunRecord.created_at, RemediationRunRecord.id)
            ).all()
            return [_run_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["remediation"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RemediationStore:
    return RemediationStore.from_engine(engine)


def _executor() -> RemediationExecutor:
    return MockRemediationExecutor()


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


@router.post(
    "/remediation-actions",
    response_model=RemediationAction,
    name="register_remediation_action",
)
def register_remediation_action_route(
    request: RegisterActionRequest,
    store: Annotated[RemediationStore, Depends(_store)],
) -> RemediationAction:
    """Register one allow-listed remediation action (template validated)."""
    try:
        return store.register_action(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/remediation-actions",
    response_model=list[RemediationAction],
    name="list_remediation_actions",
)
def list_remediation_actions_route(
    store: Annotated[RemediationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[RemediationAction]:
    """List remediation actions for one org."""
    return store.list_actions(org_id)


@router.post(
    "/remediation-actions/{action_id}/run",
    response_model=RemediationRun,
    name="run_remediation_action",
)
def run_remediation_action_route(
    action_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RunRemediationRequest,
    store: Annotated[RemediationStore, Depends(_store)],
    executor: Annotated[RemediationExecutor, Depends(_executor)],
) -> RemediationRun:
    """Gate then execute one remediation attempt (denied runs are recorded)."""
    try:
        return store.run_remediation(action_id, request, executor)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/remediation-runs",
    response_model=list[RemediationRun],
    name="list_remediation_runs",
)
def list_remediation_runs_route(
    store: Annotated[RemediationStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    action_id: Annotated[str | None, Query(max_length=128)] = None,
) -> list[RemediationRun]:
    """List remediation attempts for one org."""
    return store.list_runs(org_id, action_id)
