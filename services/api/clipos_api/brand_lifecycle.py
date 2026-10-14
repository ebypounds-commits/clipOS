"""Brand lifecycle and shutdown/archive policies (CLIP-081).

PRD §K lifecycle + shutdown policy — how a brand is operated and how
it winds down:

- **Lifecycles** — `brand_lifecycles` rows attach one guarded
  lifecycle to exactly one CLIP-078 brand template: unique natural
  key (org_id, brand_template_id) plus a client slug
  (org_id, lifecycle_id). The brand must exist in the same org (404
  otherwise). Status lifecycle (pure `lifecycle_transition_allowed`):
  DRAFT -> ACTIVE -> SUSPENDED -> RETIRED, SUSPENDED -> ACTIVE
  (resume), RETIRED terminal; same-state is a no-op; anything else
  is 409. Every real move audits
  `brand_lifecycles.status_changed` with actor/reason refs;
  registration audits `brand_lifecycles.registered`.
- **Shutdown policies** — `shutdown_policies` rows record the
  operator's declared wind-down action for one brand: KEEP_PUBLISHED
  (leave published content live), UNLIST_PUBLISHED (flip to
  unlisted on shutdown), or SCHEDULE_DELETE (mark for removal after
  grace_days 0..90). One policy per brand (unique
  (org_id, brand_template_id)); brand existence enforced (404);
  idempotent with conflict (409); audited
  `shutdown_policies.registered`. Policies declare intent only —
  enforcement is a later workflow concern.

Migration 0059 (CREATE-only). Routes (CLIP-033 pattern): POST
/brand-lifecycles, GET /brand-lifecycles?org_id=&status=, POST
/brand-lifecycles/{lifecycle_id}/transition, POST /shutdown-policies,
GET /shutdown-policies?org_id=. No LLM, no network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.brand_templates import BrandTemplateRecord
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class BrandLifecycleStatus(str, Enum):
    """Brand operating lifecycle."""

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    RETIRED = "RETIRED"


LIFECYCLE_TRANSITIONS: dict[BrandLifecycleStatus, frozenset[BrandLifecycleStatus]] = {
    BrandLifecycleStatus.DRAFT: frozenset({BrandLifecycleStatus.ACTIVE}),
    BrandLifecycleStatus.ACTIVE: frozenset({BrandLifecycleStatus.SUSPENDED}),
    BrandLifecycleStatus.SUSPENDED: frozenset(
        {BrandLifecycleStatus.ACTIVE, BrandLifecycleStatus.RETIRED}
    ),
    BrandLifecycleStatus.RETIRED: frozenset(),
}


def lifecycle_transition_allowed(
    current: BrandLifecycleStatus, target: BrandLifecycleStatus
) -> bool:
    """Pure: legal brand lifecycle move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in LIFECYCLE_TRANSITIONS[current]


class ShutdownAction(str, Enum):
    """Declared wind-down behavior (intent only)."""

    KEEP_PUBLISHED = "KEEP_PUBLISHED"
    UNLIST_PUBLISHED = "UNLIST_PUBLISHED"
    SCHEDULE_DELETE = "SCHEDULE_DELETE"


class BrandLifecycle(BaseModel):
    """One brand's guarded lifecycle."""

    model_config = ConfigDict(frozen=True)

    lifecycle_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    brand_template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    status: BrandLifecycleStatus = BrandLifecycleStatus.DRAFT
    created_at: datetime | None = None


class ShutdownPolicy(BaseModel):
    """One brand's declared shutdown behavior."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    brand_template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    action: ShutdownAction
    grace_days: int = Field(ge=0, le=90)
    created_at: datetime | None = None


class RegisterLifecycleRequest(BaseModel):
    """Request body for registering one brand lifecycle."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    lifecycle_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    brand_template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)


class LifecycleTransitionRequest(BaseModel):
    """Request body for one lifecycle transition."""

    model_config = ConfigDict(frozen=True)

    status: BrandLifecycleStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class RegisterShutdownPolicyRequest(BaseModel):
    """Request body for registering one shutdown policy."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    policy_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    brand_template_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    action: ShutdownAction
    grace_days: int = Field(ge=0, le=90)


class BrandLifecycleRecord(Base):
    """SQL row for one brand lifecycle."""

    __tablename__ = "brand_lifecycles"
    __table_args__ = (
        sa.Index(
            "uq_brand_lifecycles_org_lifecycle",
            "org_id",
            "lifecycle_id",
            unique=True,
        ),
        sa.Index(
            "uq_brand_lifecycles_org_brand",
            "org_id",
            "brand_template_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    lifecycle_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brand_template_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ShutdownPolicyRecord(Base):
    """SQL row for one shutdown policy."""

    __tablename__ = "shutdown_policies"
    __table_args__ = (
        sa.Index(
            "uq_shutdown_policies_org_policy",
            "org_id",
            "policy_id",
            unique=True,
        ),
        sa.Index(
            "uq_shutdown_policies_org_brand",
            "org_id",
            "brand_template_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brand_template_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    action: Mapped[str] = mapped_column(sa.String(24), nullable=False)
    grace_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _lifecycle_from_row(row: BrandLifecycleRecord) -> BrandLifecycle:
    return BrandLifecycle(
        lifecycle_id=row.lifecycle_id,
        org_id=row.org_id,
        brand_template_id=row.brand_template_id,
        status=BrandLifecycleStatus(row.status),
        created_at=_aware(row.created_at),
    )


def _shutdown_from_row(row: ShutdownPolicyRecord) -> ShutdownPolicy:
    return ShutdownPolicy(
        policy_id=row.policy_id,
        org_id=row.org_id,
        brand_template_id=row.brand_template_id,
        action=ShutdownAction(row.action),
        grace_days=row.grace_days,
        created_at=_aware(row.created_at),
    )


class BrandLifecycleStore:
    """Typed persistence for brand lifecycles and shutdown policies."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> BrandLifecycleStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_lifecycle(self, request: RegisterLifecycleRequest) -> BrandLifecycle:
        """One lifecycle per brand; brand must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            brand = session.scalars(
                sa.select(BrandTemplateRecord).where(
                    BrandTemplateRecord.org_id == request.org_id,
                    BrandTemplateRecord.template_id == request.brand_template_id,
                )
            ).first()
            if brand is None:
                raise ValueError(f"brand template not found: {request.brand_template_id}")
            existing = session.scalars(
                sa.select(BrandLifecycleRecord).where(
                    BrandLifecycleRecord.org_id == request.org_id,
                    BrandLifecycleRecord.lifecycle_id == request.lifecycle_id,
                )
            ).first()
            if existing is not None:
                stored = _lifecycle_from_row(existing)
                if stored.brand_template_id != request.brand_template_id:
                    raise ValueError(
                        f"lifecycle already registered with different fields: "
                        f"{request.lifecycle_id}"
                    )
                return stored
            twin = session.scalars(
                sa.select(BrandLifecycleRecord).where(
                    BrandLifecycleRecord.org_id == request.org_id,
                    BrandLifecycleRecord.brand_template_id == request.brand_template_id,
                )
            ).first()
            if twin is not None:
                raise ValueError(
                    f"lifecycle already registered for brand: {request.brand_template_id}"
                )
            row = BrandLifecycleRecord(
                lifecycle_id=request.lifecycle_id,
                org_id=request.org_id,
                brand_template_id=request.brand_template_id,
                status=BrandLifecycleStatus.DRAFT.value,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "brand_lifecycles.registered",
                "brand_lifecycles",
                f"brand_lifecycle:{request.lifecycle_id}",
                {
                    "org_id": request.org_id,
                    "brand_template_id": request.brand_template_id,
                },
            )
            return _lifecycle_from_row(row)

    def list_lifecycles(
        self, org_id: str, status: BrandLifecycleStatus | None = None
    ) -> list[BrandLifecycle]:
        with self._sessions() as session:
            stmt = sa.select(BrandLifecycleRecord).where(BrandLifecycleRecord.org_id == org_id)
            if status is not None:
                stmt = stmt.where(BrandLifecycleRecord.status == status.value)
            rows = session.scalars(
                stmt.order_by(BrandLifecycleRecord.created_at, BrandLifecycleRecord.id)
            ).all()
            return [_lifecycle_from_row(r) for r in rows]

    def transition_lifecycle(
        self, lifecycle_id: str, org_id: str, request: LifecycleTransitionRequest
    ) -> BrandLifecycle:
        """Apply a validated lifecycle move (404 unknown, 409 illegal)."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(BrandLifecycleRecord).where(
                    BrandLifecycleRecord.lifecycle_id == lifecycle_id,
                    BrandLifecycleRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"brand lifecycle not found: {lifecycle_id}")
            current = BrandLifecycleStatus(row.status)
            target = request.status
            if not lifecycle_transition_allowed(current, target):
                raise ValueError(f"illegal lifecycle transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "brand_lifecycles.status_changed",
                    "brand_lifecycles",
                    f"brand_lifecycle:{lifecycle_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _lifecycle_from_row(row)

    def register_shutdown(self, request: RegisterShutdownPolicyRequest) -> ShutdownPolicy:
        """One policy per brand; brand must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            brand = session.scalars(
                sa.select(BrandTemplateRecord).where(
                    BrandTemplateRecord.org_id == request.org_id,
                    BrandTemplateRecord.template_id == request.brand_template_id,
                )
            ).first()
            if brand is None:
                raise ValueError(f"brand template not found: {request.brand_template_id}")
            existing = session.scalars(
                sa.select(ShutdownPolicyRecord).where(
                    ShutdownPolicyRecord.org_id == request.org_id,
                    ShutdownPolicyRecord.policy_id == request.policy_id,
                )
            ).first()
            if existing is not None:
                stored = _shutdown_from_row(existing)
                if (
                    stored.brand_template_id != request.brand_template_id
                    or stored.action != request.action
                    or stored.grace_days != request.grace_days
                ):
                    raise ValueError(
                        f"shutdown policy already registered with different fields: "
                        f"{request.policy_id}"
                    )
                return stored
            twin = session.scalars(
                sa.select(ShutdownPolicyRecord).where(
                    ShutdownPolicyRecord.org_id == request.org_id,
                    ShutdownPolicyRecord.brand_template_id == request.brand_template_id,
                )
            ).first()
            if twin is not None:
                raise ValueError(
                    f"shutdown policy already registered for brand: {request.brand_template_id}"
                )
            row = ShutdownPolicyRecord(
                policy_id=request.policy_id,
                org_id=request.org_id,
                brand_template_id=request.brand_template_id,
                action=request.action.value,
                grace_days=request.grace_days,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "shutdown_policies.registered",
                "shutdown_policies",
                f"shutdown_policy:{request.policy_id}",
                {
                    "org_id": request.org_id,
                    "brand_template_id": request.brand_template_id,
                    "action": request.action.value,
                    "grace_days": request.grace_days,
                },
            )
            return _shutdown_from_row(row)

    def list_shutdowns(self, org_id: str) -> list[ShutdownPolicy]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ShutdownPolicyRecord)
                .where(ShutdownPolicyRecord.org_id == org_id)
                .order_by(ShutdownPolicyRecord.created_at, ShutdownPolicyRecord.id)
            ).all()
            return [_shutdown_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["brand-lifecycle"])


def _store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> BrandLifecycleStore:
    return BrandLifecycleStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        return HTTPException(status_code=404, detail=message)
    if "already" in message or "illegal" in message:
        return HTTPException(status_code=409, detail=message)
    return HTTPException(status_code=422, detail=message)


@router.post(
    "/brand-lifecycles",
    response_model=BrandLifecycle,
    name="register_brand_lifecycle",
)
def register_brand_lifecycle_route(
    request: RegisterLifecycleRequest,
    store: Annotated[BrandLifecycleStore, Depends(_store)],
) -> BrandLifecycle:
    """Register one brand lifecycle (one per brand; brand must exist)."""
    try:
        return store.register_lifecycle(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/brand-lifecycles",
    response_model=list[BrandLifecycle],
    name="list_brand_lifecycles",
)
def list_brand_lifecycles_route(
    store: Annotated[BrandLifecycleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    status: Annotated[BrandLifecycleStatus | None, Query()] = None,
) -> list[BrandLifecycle]:
    """List brand lifecycles for one org."""
    return store.list_lifecycles(org_id, status)


@router.post(
    "/brand-lifecycles/{lifecycle_id}/transition",
    response_model=BrandLifecycle,
    name="transition_brand_lifecycle",
)
def transition_brand_lifecycle_route(
    lifecycle_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: LifecycleTransitionRequest,
    store: Annotated[BrandLifecycleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> BrandLifecycle:
    """Apply a lifecycle transition (404 unknown, 409 illegal)."""
    try:
        return store.transition_lifecycle(lifecycle_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/shutdown-policies",
    response_model=ShutdownPolicy,
    name="register_shutdown_policy",
)
def register_shutdown_policy_route(
    request: RegisterShutdownPolicyRequest,
    store: Annotated[BrandLifecycleStore, Depends(_store)],
) -> ShutdownPolicy:
    """Register one shutdown policy (one per brand; intent only)."""
    try:
        return store.register_shutdown(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/shutdown-policies",
    response_model=list[ShutdownPolicy],
    name="list_shutdown_policies",
)
def list_shutdown_policies_route(
    store: Annotated[BrandLifecycleStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[ShutdownPolicy]:
    """List shutdown policies for one org."""
    return store.list_shutdowns(org_id)
