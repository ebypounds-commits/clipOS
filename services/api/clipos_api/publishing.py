"""Publishing adapter contract and private-test policy (CLIP-058).

The M14 foundation from `08_INTEGRATIONS` + PRD §7 operating modes:

- **Adapter contract** — provider-neutral ``PublishingAdapter`` Protocol
  (health_check with explicit capabilities, publish, get_status;
  normalized results, timeout-bound inside implementations) with
  ``MockPublishingAdapter`` as the deterministic CI stand-in (external
  ids derived from the idempotency key — no network, no vendor id).
  Real platform adapters (CLIP-059/060/061) register behind this
  Protocol, selected per platform — platform slugs are generic names
  (youtube/tiktok), never provider credentials.
- **Private-test policy** — `publish_policies` (one per org+platform)
  encodes allowed visibilities (default **private/unlisted only**),
  `public_enabled` (default FALSE — PRD: public posting disabled by
  default), and a `kill_switch` (PRD: globally disable without
  redeploy). `evaluate_publish_policy` is the pure fail-closed
  evaluator: kill switch denies everything; OBSERVE/DRAFT modes deny
  all publishing; PRIVATE_TEST allows only PRIVATE/UNLISTED; PUBLIC
  additionally requires `public_enabled`; anything outside
  `allowed_visibilities` denies. Unknown/missing policy -> 404 (explicit
  opt-in required).
- **Submission flow** — `submit_publication` evaluates the policy
  first: DENIED submissions persist an auditable DENIED job (decision
  reason recorded, adapter NEVER called — zero external side effect) and
  return it with status DENIED; ALLOWED submissions call the adapter
  once and persist SUCCEEDED/FAILED with the external id. Repeat
  submissions with the same `(org_id, idempotency_key)` return the
  stored job without re-publishing (external-side-effect idempotency).

Audits: `publish_policies.registered`, `publication_jobs.submitted`,
`publication_jobs.denied`. Routes: policy register/get/list, publish
submission, job fetch. The caller-supplied `operating_mode` is the
contract seam until the runtime mode binds to server ops config (M23);
PUBLIC blocking and kill switch enforce regardless of mode claims. No
LLM, no provider identifier anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha1
from typing import Annotated, Protocol
from uuid import uuid4

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base


class Visibility(str, Enum):
    """Publication visibility levels (platform-generic)."""

    PRIVATE = "PRIVATE"
    UNLISTED = "UNLISTED"
    PUBLIC = "PUBLIC"


class OperatingMode(str, Enum):
    """PRD §7 autonomous operating modes."""

    OBSERVE = "OBSERVE"
    DRAFT = "DRAFT"
    PRIVATE_TEST = "PRIVATE_TEST"
    GUARDED_PUBLIC = "GUARDED_PUBLIC"
    AUTONOMOUS_PUBLIC = "AUTONOMOUS_PUBLIC"


class PublishStatus(str, Enum):
    """Normalized publication status across providers."""

    SUCCEEDED = "SUCCEEDED"
    DENIED = "DENIED"
    FAILED = "FAILED"


class AdapterHealth(BaseModel):
    """Normalized health probe result for one publishing adapter."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    capabilities: list[str] = Field(default_factory=list)
    detail: str | None = Field(default=None, max_length=255)


class PublishRequest(BaseModel):
    """Provider-neutral publish request handed to an adapter."""

    model_config = ConfigDict(frozen=True)

    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    visibility: Visibility
    idempotency_key: str = Field(min_length=1, max_length=255)


class PublishResult(BaseModel):
    """Normalized adapter publish result."""

    model_config = ConfigDict(frozen=True)

    external_id: str = Field(min_length=1, max_length=255)
    status: PublishStatus = PublishStatus.SUCCEEDED


class PublishingAdapter(Protocol):
    """Provider-neutral publishing backend (08 integration contract)."""

    def health_check(self, account_ref: str) -> AdapterHealth: ...

    def publish(self, request: PublishRequest) -> PublishResult: ...

    def get_status(self, external_id: str) -> PublishStatus: ...


class MockPublishingAdapter:
    """Deterministic CI stand-in: no network; ids derive from the key."""

    def health_check(self, account_ref: str) -> AdapterHealth:
        return AdapterHealth(ok=True, capabilities=["upload", "status"], detail="mock")

    def publish(self, request: PublishRequest) -> PublishResult:
        digest = sha1(request.idempotency_key.encode()).hexdigest()[:12]
        return PublishResult(external_id=f"mock-{digest}", status=PublishStatus.SUCCEEDED)

    def get_status(self, external_id: str) -> PublishStatus:
        return PublishStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# Adapter registry — platform adapters register here via register_adapter.
# MockPublishingAdapter is the fallback for any unregistered platform.
# YouTube registers at module-import time in youtube_adapter.py.
# TikTok/Meta follow in later M14 tickets.
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, PublishingAdapter] = {"_default": MockPublishingAdapter()}


def register_adapter(platform: str, adapter: PublishingAdapter) -> None:
    """Register a platform-specific adapter (replaces Mock for that platform)."""
    _ADAPTERS[platform] = adapter


def get_adapter(platform: str) -> PublishingAdapter:
    """Return the adapter for a platform key, falling back to Mock."""
    return _ADAPTERS.get(platform, _ADAPTERS["_default"])


class PublishPolicy(BaseModel):
    """One org+platform publishing policy; public disabled by default."""

    model_config = ConfigDict(frozen=True)

    policy_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    allowed_visibilities: list[Visibility] = Field(
        default_factory=lambda: [Visibility.PRIVATE, Visibility.UNLISTED]
    )
    public_enabled: bool = False
    kill_switch: bool = False
    created_at: datetime | None = None


class PublishSubmission(BaseModel):
    """Request body for one publication submission."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    visibility: Visibility
    idempotency_key: str = Field(min_length=1, max_length=255)
    operating_mode: OperatingMode


class PublicationJob(BaseModel):
    """One recorded publication attempt (auditable outcome)."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    platform: str = Field(min_length=1, max_length=64)
    account_ref: str = Field(min_length=1, max_length=255)
    content_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    visibility: Visibility
    operating_mode: OperatingMode
    idempotency_key: str = Field(min_length=1, max_length=255)
    status: PublishStatus
    decision_reason: str = Field(min_length=1, max_length=255)
    external_id: str | None = Field(default=None, max_length=255)
    error_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


def evaluate_publish_policy(
    policy: PublishPolicy,
    visibility: Visibility,
    operating_mode: OperatingMode,
) -> tuple[bool, str]:
    """Pure fail-closed policy evaluation (PRD §7 + defaults).

    Returns (allowed, reason). Kill switch denies everything; OBSERVE and
    DRAFT never publish; PRIVATE_TEST allows only PRIVATE/UNLISTED;
    PUBLIC requires public_enabled; visibility must also be inside the
    policy's allowed set.
    """
    if policy.kill_switch:
        return False, "kill_switch_active"
    if operating_mode in (OperatingMode.OBSERVE, OperatingMode.DRAFT):
        return False, f"mode_{operating_mode.value}_denies_publishing"
    if visibility not in policy.allowed_visibilities:
        return False, "visibility_not_allowed"
    if visibility is Visibility.PUBLIC:
        if operating_mode is OperatingMode.PRIVATE_TEST:
            return False, "private_test_forbids_public"
        if not policy.public_enabled:
            return False, "public_disabled_by_default"
    return True, "allowed"


class PublishPolicyRecord(Base):
    """SQL row for one publishing policy."""

    __tablename__ = "publish_policies"
    __table_args__ = (
        sa.Index("uq_publish_policies_org_policy", "org_id", "policy_id", unique=True),
        sa.Index("uq_publish_policies_org_platform", "org_id", "platform", unique=True),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    policy_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    allowed_visibilities: Mapped[list] = mapped_column(sa.JSON, nullable=False)
    public_enabled: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    kill_switch: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class PublicationJobRecord(Base):
    """SQL row for one publication attempt."""

    __tablename__ = "publication_jobs"
    __table_args__ = (
        sa.Index("uq_publication_jobs_id", "job_id", unique=True),
        sa.Index(
            "uq_publication_jobs_org_idempotency",
            "org_id",
            "idempotency_key",
            unique=True,
        ),
        sa.Index("ix_publication_jobs_org_platform", "org_id", "platform"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    account_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    content_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    visibility: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    operating_mode: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    decision_reason: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    error_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _policy_from_row(row: PublishPolicyRecord) -> PublishPolicy:
    return PublishPolicy(
        policy_id=row.policy_id,
        org_id=row.org_id,
        platform=row.platform,
        allowed_visibilities=[Visibility(v) for v in (row.allowed_visibilities or [])],
        public_enabled=row.public_enabled,
        kill_switch=row.kill_switch,
        created_at=_aware(row.created_at),
    )


def _job_from_row(row: PublicationJobRecord) -> PublicationJob:
    return PublicationJob(
        job_id=row.job_id,
        org_id=row.org_id,
        platform=row.platform,
        account_ref=row.account_ref,
        content_ref=row.content_ref,
        title=row.title,
        visibility=Visibility(row.visibility),
        operating_mode=OperatingMode(row.operating_mode),
        idempotency_key=row.idempotency_key,
        status=PublishStatus(row.status),
        decision_reason=row.decision_reason,
        external_id=row.external_id,
        error_ref=row.error_ref,
        created_at=_aware(row.created_at),
    )


class PublishingStore:
    """Typed persistence for publish policies and publication jobs."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> PublishingStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_policy(self, policy: PublishPolicy) -> PublishPolicy:
        """Idempotent register per (org, policy_id); one policy per platform."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(PublishPolicyRecord).where(
                    PublishPolicyRecord.org_id == policy.org_id,
                    PublishPolicyRecord.policy_id == policy.policy_id,
                )
            ).first()
            if existing is not None:
                stored = _policy_from_row(existing)
                if (
                    stored.platform != policy.platform
                    or stored.allowed_visibilities != policy.allowed_visibilities
                    or stored.public_enabled != policy.public_enabled
                    or stored.kill_switch != policy.kill_switch
                ):
                    raise ValueError(
                        f"publish policy already registered with different fields: "
                        f"{policy.policy_id}"
                    )
                return stored
            platform_twin = session.scalars(
                sa.select(PublishPolicyRecord).where(
                    PublishPolicyRecord.org_id == policy.org_id,
                    PublishPolicyRecord.platform == policy.platform,
                )
            ).first()
            if platform_twin is not None:
                raise ValueError(
                    f"publish policy already registered with different fields: "
                    f"platform {policy.platform}"
                )
            row = PublishPolicyRecord(
                policy_id=policy.policy_id,
                org_id=policy.org_id,
                platform=policy.platform,
                allowed_visibilities=[v.value for v in policy.allowed_visibilities],
                public_enabled=policy.public_enabled,
                kill_switch=policy.kill_switch,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "publish_policies.registered",
                "publish_policies",
                f"publish_policy:{policy.policy_id}",
                {
                    "org_id": policy.org_id,
                    "platform": policy.platform,
                    "public_enabled": policy.public_enabled,
                    "kill_switch": policy.kill_switch,
                },
            )
            return _policy_from_row(row)

    def get_policy(self, policy_id: str, org_id: str) -> PublishPolicy | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PublishPolicyRecord).where(
                    PublishPolicyRecord.policy_id == policy_id,
                    PublishPolicyRecord.org_id == org_id,
                )
            ).first()
            return _policy_from_row(row) if row is not None else None

    def get_policy_for_platform(self, platform: str, org_id: str) -> PublishPolicy | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PublishPolicyRecord).where(
                    PublishPolicyRecord.platform == platform,
                    PublishPolicyRecord.org_id == org_id,
                )
            ).first()
            return _policy_from_row(row) if row is not None else None

    def list_policies(self, org_id: str) -> list[PublishPolicy]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(PublishPolicyRecord)
                .where(PublishPolicyRecord.org_id == org_id)
                .order_by(PublishPolicyRecord.created_at, PublishPolicyRecord.id)
            ).all()
            return [_policy_from_row(row) for row in rows]

    def get_job(self, job_id: str, org_id: str) -> PublicationJob | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(PublicationJobRecord).where(
                    PublicationJobRecord.job_id == job_id,
                    PublicationJobRecord.org_id == org_id,
                )
            ).first()
            return _job_from_row(row) if row is not None else None


def submit_publication(
    adapter: PublishingAdapter,
    store: PublishingStore,
    request: PublishSubmission,
) -> PublicationJob:
    """Evaluate policy then publish once (idempotent, fail-closed).

    Missing policy -> 404 (explicit opt-in required). DENIED submissions
    persist an audited DENIED job with the decision reason and never call
    the adapter. Repeat submissions on the same (org, idempotency_key)
    return the stored job without re-publishing (the unique
    (org, idempotency_key) index arbitrates races; network adapters wire
    idempotency.execute_idempotent when they gain real side effects —
    CLIP-059 follow-up). The adapter runs OUTSIDE any DB transaction so a
    SQLite test engine never nests write locks; the job insert re-checks
    idempotency at commit time. Adapter failures persist a FAILED job.
    """
    now = datetime.now(UTC)

    # Read-only phase: idempotency replay + policy lookup.
    with store._sessions() as session:
        existing = session.scalars(
            sa.select(PublicationJobRecord).where(
                PublicationJobRecord.org_id == request.org_id,
                PublicationJobRecord.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            stored = _job_from_row(existing)
            if (
                stored.platform != request.platform
                or stored.content_ref != request.content_ref
                or stored.visibility != request.visibility
            ):
                raise ValueError(
                    f"publication already recorded with different fields: {request.idempotency_key}"
                )
            return stored
        policy_row = session.scalars(
            sa.select(PublishPolicyRecord).where(
                PublishPolicyRecord.org_id == request.org_id,
                PublishPolicyRecord.platform == request.platform,
            )
        ).first()
        if policy_row is None:
            raise ValueError(f"publish policy not found: {request.org_id}/{request.platform}")
        policy = _policy_from_row(policy_row)

    allowed, reason = evaluate_publish_policy(policy, request.visibility, request.operating_mode)

    job_id = str(uuid4())[:12]
    if not allowed:
        status = PublishStatus.DENIED
        external_id: str | None = None
        error_ref: str | None = None
        audit_action = "publication_jobs.denied"
    else:
        try:
            result = adapter.publish(
                PublishRequest(
                    account_ref=request.account_ref,
                    content_ref=request.content_ref,
                    title=request.title,
                    visibility=request.visibility,
                    idempotency_key=request.idempotency_key,
                )
            )
            status = PublishStatus.SUCCEEDED
            external_id = result.external_id
            error_ref = None
            audit_action = "publication_jobs.submitted"
        except Exception as exc:  # noqa: BLE001 - 08 normalized adapter errors
            status = PublishStatus.FAILED
            external_id = None
            error_ref = str(exc)[:512]
            audit_action = "publication_jobs.submitted"

    # Short write transaction; unique index re-arbitrates concurrent duplicates.
    with store._sessions.begin() as session:
        existing = session.scalars(
            sa.select(PublicationJobRecord).where(
                PublicationJobRecord.org_id == request.org_id,
                PublicationJobRecord.idempotency_key == request.idempotency_key,
            )
        ).first()
        if existing is not None:
            return _job_from_row(existing)
        row = PublicationJobRecord(
            job_id=job_id,
            org_id=request.org_id,
            platform=request.platform,
            account_ref=request.account_ref,
            content_ref=request.content_ref,
            title=request.title,
            visibility=request.visibility.value,
            operating_mode=request.operating_mode.value,
            idempotency_key=request.idempotency_key,
            status=status.value,
            decision_reason=reason,
            external_id=external_id,
            error_ref=error_ref,
            created_at=now,
        )
        session.add(row)
        session.flush()
        audit.record(
            audit_action,
            "publication_jobs",
            f"publication_job:{job_id}",
            {
                "org_id": request.org_id,
                "platform": request.platform,
                "status": status.value,
                "reason": reason,
                "visibility": request.visibility.value,
                "operating_mode": request.operating_mode.value,
                "external_id": external_id,
            },
        )
        return _job_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["publishing"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> PublishingStore:
    return PublishingStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def _map_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    if "not found" in message:
        status = 404
    elif "already" in message:
        status = 409
    else:
        status = 422
    return HTTPException(status_code=status, detail=message)


@router.post(
    "/publish-policies",
    response_model=PublishPolicy,
    name="register_publish_policy",
)
def register_publish_policy_route(
    request: PublishPolicy,
    store: Annotated[PublishingStore, Depends(_store)],
) -> PublishPolicy:
    """Register one org+platform publishing policy (idempotent)."""
    try:
        return store.register_policy(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/publish-policies/{policy_id}",
    response_model=PublishPolicy,
    name="get_publish_policy",
)
def get_publish_policy_route(
    policy_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[PublishingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> PublishPolicy:
    """Get one publishing policy by id and org."""
    stored = store.get_policy(policy_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"publish policy not found: {policy_id}")
    return stored


@router.get(
    "/publish-policies",
    response_model=list[PublishPolicy],
    name="list_publish_policies",
)
def list_publish_policies_route(
    store: Annotated[PublishingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[PublishPolicy]:
    """List publishing policies for one org."""
    return store.list_policies(org_id)


@router.post(
    "/publications",
    response_model=PublicationJob,
    name="submit_publication",
)
def submit_publication_route(
    request: PublishSubmission,
    store: Annotated[PublishingStore, Depends(_store)],
) -> PublicationJob:
    """Evaluate policy then publish once (Mock adapter; idempotent).

    DENIED submissions return a DENIED job (200) with the decision
    reason — the attempt is recorded and the adapter never runs.
    """
    try:
        return submit_publication(get_adapter(request.platform), store, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/publications/{job_id}",
    response_model=PublicationJob,
    name="get_publication_job",
)
def get_publication_job_route(
    job_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[PublishingStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> PublicationJob:
    """Get one publication job by id and org."""
    stored = store.get_job(job_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"publication job not found: {job_id}")
    return stored
