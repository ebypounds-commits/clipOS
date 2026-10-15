"""Threat model and abuse cases (CLIP-090, M22 opener).

A machine-readable threat model grounded in `07_SECURITY_GOVERNANCE_RIGHTS.md`:
each threat is a stored record with an abuse case, the controls that
mitigate it, and a verifiable mapping to code that enforces those
controls — so drift between the document and the implementation is
detectable.

- **Threat records** — `threats` are org-scoped, idempotent per
  (org_id, threat_id) with slug ids: a `category` (STRIDE-style:
  SPOOFING, TAMPERING, REPUDIATION, INFORMATION_DISCLOSURE,
  DENIAL_OF_SERVICE, ELEVATION_OF_PRIVILEGE), an abuse case (the
  concrete attack narrative), severity, and status
  (IDENTIFIED / MITIGATED / ACCEPTED). Linked controls live in
  `threat_controls` (append-only, unique (org_id, threat_id,
  control_id)) and named mitigations in `threat_mitigations`
  (append-only, unique (org, threat_id, mitigation_id)) carrying the
  control_ref and the enforcing `evidence_ref` (module/route/gate).
- **Coverage audit** — `audit_threat_coverage` is pure: a threat is
  `covered` only when it has at least one mitigation whose
  `evidence_ref` resolves to a real module in the codebase and whose
  control_ref is non-empty; MITIGATED threats without such evidence are
  reported as `unevidenced`. The audit returns per-threat findings plus
  counts, so an operator can see which claims are unbacked.
- **Abuse cases** — `abuse_cases` records are the same shape with an
  `attack_surface` and expected `detection_ref`, reviewed with the
  same lifecycle. Audited `threats.registered`,
  `threats.status_changed`, `abuse_cases.registered`.

Migration 0067 (CREATE-only). Routes (CLIP-033 pattern): POST/GET
/threats, GET /threats/{threat_id}, POST /threats/{threat_id}/mitigations,
POST /threats/{threat_id}/transition, GET /threats/coverage, POST/GET
/abuse-cases. No LLM, no network.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base


class ThreatCategory(str, Enum):
    """STRIDE-style threat categories."""

    SPOOFING = "SPOOFING"
    TAMPERING = "TAMPERING"
    REPUDIATION = "REPUDIATION"
    INFORMATION_DISCLOSURE = "INFORMATION_DISCLOSURE"
    DENIAL_OF_SERVICE = "DENIAL_OF_SERVICE"
    ELEVATION_OF_PRIVILEGE = "ELEVATION_OF_PRIVILEGE"


class ThreatStatus(str, Enum):
    """Threat review lifecycle."""

    IDENTIFIED = "IDENTIFIED"
    MITIGATED = "MITIGATED"
    ACCEPTED = "ACCEPTED"


class Severity(str, Enum):
    """Threat severity."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


_THREAT_TRANSITIONS: dict[ThreatStatus, frozenset[ThreatStatus]] = {
    ThreatStatus.IDENTIFIED: frozenset({ThreatStatus.MITIGATED, ThreatStatus.ACCEPTED}),
    ThreatStatus.MITIGATED: frozenset({ThreatStatus.IDENTIFIED}),
    ThreatStatus.ACCEPTED: frozenset({ThreatStatus.IDENTIFIED}),
}


def threat_transition_allowed(current: ThreatStatus, target: ThreatStatus) -> bool:
    """Pure: legal threat status move (same-state counts as no-op)."""
    if current is target:
        return True
    return target in _THREAT_TRANSITIONS[current]


class Threat(BaseModel):
    """One modelled threat with its abuse case."""

    model_config = ConfigDict(frozen=True)

    threat_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    category: ThreatCategory
    severity: Severity
    abuse_case: str = Field(min_length=1, max_length=2000)
    status: ThreatStatus = ThreatStatus.IDENTIFIED
    controls: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class ThreatMitigation(BaseModel):
    """One named mitigation mapped to a control and enforcing code."""

    model_config = ConfigDict(frozen=True)

    mitigation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    threat_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    control_ref: str = Field(min_length=1, max_length=512)
    evidence_ref: str = Field(min_length=1, max_length=512)
    note: str = Field(default="", max_length=1000)
    created_at: datetime | None = None


class AbuseCase(BaseModel):
    """One abuse case with its attack surface and detection."""

    model_config = ConfigDict(frozen=True)

    abuse_case_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    attack_surface: str = Field(min_length=1, max_length=512)
    scenario: str = Field(min_length=1, max_length=2000)
    detection_ref: str | None = Field(default=None, max_length=512)
    threat_id: str | None = Field(default=None, max_length=128)
    created_at: datetime | None = None


class RegisterThreatRequest(BaseModel):
    """Request body for registering one threat."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    threat_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    category: ThreatCategory
    severity: Severity
    abuse_case: str = Field(min_length=1, max_length=2000)
    controls: list[str] = Field(default_factory=list, max_length=32)


class RegisterMitigationRequest(BaseModel):
    """Request body for one mitigation mapping."""

    model_config = ConfigDict(frozen=True)

    mitigation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    control_ref: str = Field(min_length=1, max_length=512)
    evidence_ref: str = Field(min_length=1, max_length=512)
    note: str = Field(default="", max_length=1000)


class ThreatTransitionRequest(BaseModel):
    """Request body for one threat status move."""

    model_config = ConfigDict(frozen=True)

    status: ThreatStatus
    actor_ref: str = Field(min_length=1, max_length=255)
    reason_ref: str | None = Field(default=None, max_length=512)


class RegisterAbuseCaseRequest(BaseModel):
    """Request body for registering one abuse case."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    abuse_case_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    title: str = Field(min_length=1, max_length=255)
    attack_surface: str = Field(min_length=1, max_length=512)
    scenario: str = Field(min_length=1, max_length=2000)
    detection_ref: str | None = Field(default=None, max_length=512)
    threat_id: str | None = Field(default=None, max_length=128)


class ThreatFinding(BaseModel):
    """Coverage verdict for one threat."""

    model_config = ConfigDict(frozen=True)

    threat_id: str
    status: str
    severity: str
    mitigation_count: int
    evidenced_mitigation_count: int
    verdict: str
    missing: list[str] = Field(default_factory=list)


class CoverageReport(BaseModel):
    """Aggregate coverage over every stored threat."""

    model_config = ConfigDict(frozen=True)

    org_id: str
    total_threats: int
    covered: int
    unevidenced: int
    uncovered: int
    findings: list[ThreatFinding] = Field(default_factory=list)


def _module_exists(evidence_ref: str) -> bool:
    """True when an evidence_ref names a real, importable clipos_api module.

    Accepts `clipos_api.foo`, `clipos_api/foo.py`, or `services/api/...`.
    """
    text = evidence_ref.strip()
    for prefix in ("services/api/", "services/api\\", "./"):
        text = text.removeprefix(prefix)
    text = text.removesuffix(".py")
    text = text.replace("/", ".").replace("\\", ".")
    if not text.startswith("clipos_api"):
        return False
    try:
        return importlib.util.find_spec(text) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def audit_threat_coverage(
    org_id: str,
    threats: list[Threat],
    mitigations: list[ThreatMitigation],
) -> CoverageReport:
    """Pure: which threats are genuinely covered by evidenced mitigations.

    covered      -> at least one mitigation whose evidence_ref resolves
                    to a real module and whose control_ref is non-empty
    unevidenced  -> mitigations exist but none resolve to real code
    uncovered    -> no mitigations at all
    """
    findings: list[ThreatFinding] = []
    covered = unevidenced = uncovered = 0
    for threat in threats:
        own = [m for m in mitigations if m.threat_id == threat.threat_id]
        good = [m for m in own if m.control_ref.strip() and _module_exists(m.evidence_ref)]
        if good:
            verdict = "covered"
            covered += 1
        elif own:
            verdict = "unevidenced"
            unevidenced += 1
        else:
            verdict = "uncovered"
            uncovered += 1
        findings.append(
            ThreatFinding(
                threat_id=threat.threat_id,
                status=threat.status.value,
                severity=threat.severity.value,
                mitigation_count=len(own),
                evidenced_mitigation_count=len(good),
                verdict=verdict,
                missing=[m.mitigation_id for m in own if m not in good],
            )
        )
    return CoverageReport(
        org_id=org_id,
        total_threats=len(threats),
        covered=covered,
        unevidenced=unevidenced,
        uncovered=uncovered,
        findings=findings,
    )


class ThreatRecord(Base):
    """SQL row for one threat."""

    __tablename__ = "threats"
    __table_args__ = (
        sa.Index("uq_threats_org_threat", "org_id", "threat_id", unique=True),
        sa.Index("ix_threats_org_category", "org_id", "category"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    threat_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    category: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    severity: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    abuse_case: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    controls: Mapped[list[Any]] = mapped_column(sa.JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ThreatControlRecord(Base):
    """SQL row for one declared control name (append-only)."""

    __tablename__ = "threat_controls"
    __table_args__ = (
        sa.Index(
            "uq_threat_controls_identity",
            "org_id",
            "threat_id",
            "control_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    threat_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    control_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ThreatMitigationRecord(Base):
    """SQL row for one mitigation mapping (append-only)."""

    __tablename__ = "threat_mitigations"
    __table_args__ = (
        sa.Index(
            "uq_threat_mitigations_identity",
            "org_id",
            "threat_id",
            "mitigation_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    mitigation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    threat_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    control_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    note: Mapped[str] = mapped_column(sa.String(1000), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class AbuseCaseRecord(Base):
    """SQL row for one abuse case."""

    __tablename__ = "abuse_cases"
    __table_args__ = (
        sa.Index("uq_abuse_cases_org_case", "org_id", "abuse_case_id", unique=True),
        sa.Index("ix_abuse_cases_org_surface", "org_id", "attack_surface"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    abuse_case_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    attack_surface: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    scenario: Mapped[str] = mapped_column(sa.String(2000), nullable=False)
    detection_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    threat_id: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _threat_from_row(row: ThreatRecord) -> Threat:
    return Threat(
        threat_id=row.threat_id,
        org_id=row.org_id,
        title=row.title,
        category=ThreatCategory(row.category),
        severity=Severity(row.severity),
        abuse_case=row.abuse_case,
        status=ThreatStatus(row.status),
        controls=[str(c) for c in (row.controls or [])],
        created_at=_aware(row.created_at),
    )


def _mitigation_from_row(row: ThreatMitigationRecord) -> ThreatMitigation:
    return ThreatMitigation(
        mitigation_id=row.mitigation_id,
        org_id=row.org_id,
        threat_id=row.threat_id,
        control_ref=row.control_ref,
        evidence_ref=row.evidence_ref,
        note=row.note,
        created_at=_aware(row.created_at),
    )


def _abuse_from_row(row: AbuseCaseRecord) -> AbuseCase:
    return AbuseCase(
        abuse_case_id=row.abuse_case_id,
        org_id=row.org_id,
        title=row.title,
        attack_surface=row.attack_surface,
        scenario=row.scenario,
        detection_ref=row.detection_ref,
        threat_id=row.threat_id,
        created_at=_aware(row.created_at),
    )


class ThreatStore:
    """Typed persistence for the threat model."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ThreatStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_threat(self, request: RegisterThreatRequest) -> Threat:
        """Idempotent per (org, threat_id); control names append-only."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(ThreatRecord).where(
                    ThreatRecord.org_id == request.org_id,
                    ThreatRecord.threat_id == request.threat_id,
                )
            ).first()
            if existing is not None:
                stored = _threat_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.category != request.category
                    or stored.severity != request.severity
                    or stored.abuse_case != request.abuse_case
                    or stored.controls != request.controls
                ):
                    raise ValueError(
                        f"threat already registered with different fields: {request.threat_id}"
                    )
                return stored
            row = ThreatRecord(
                threat_id=request.threat_id,
                org_id=request.org_id,
                title=request.title,
                category=request.category.value,
                severity=request.severity.value,
                abuse_case=request.abuse_case,
                status=ThreatStatus.IDENTIFIED.value,
                controls=list(request.controls),
                created_at=now,
            )
            session.add(row)
            session.flush()
            for control in request.controls:
                session.add(
                    ThreatControlRecord(
                        org_id=request.org_id,
                        threat_id=request.threat_id,
                        control_id=control[:128],
                        created_at=now,
                    )
                )
            session.flush()
            audit.record(
                "threats.registered",
                "threats",
                f"threat:{request.threat_id}",
                {
                    "org_id": request.org_id,
                    "category": request.category.value,
                    "severity": request.severity.value,
                    "control_count": len(request.controls),
                },
            )
            return _threat_from_row(row)

    def get_threat(self, threat_id: str, org_id: str) -> Threat | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(ThreatRecord).where(
                    ThreatRecord.threat_id == threat_id,
                    ThreatRecord.org_id == org_id,
                )
            ).first()
            return _threat_from_row(row) if row is not None else None

    def list_threats(self, org_id: str, category: ThreatCategory | None = None) -> list[Threat]:
        with self._sessions() as session:
            stmt = sa.select(ThreatRecord).where(ThreatRecord.org_id == org_id)
            if category is not None:
                stmt = stmt.where(ThreatRecord.category == category.value)
            rows = session.scalars(stmt.order_by(ThreatRecord.created_at, ThreatRecord.id)).all()
            return [_threat_from_row(r) for r in rows]

    def register_mitigation(
        self, threat_id: str, org_id: str, request: RegisterMitigationRequest
    ) -> ThreatMitigation:
        """Attach one mitigation; threat must exist (404)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            threat = session.scalars(
                sa.select(ThreatRecord).where(
                    ThreatRecord.threat_id == threat_id,
                    ThreatRecord.org_id == org_id,
                )
            ).first()
            if threat is None:
                raise ValueError(f"threat not found: {threat_id}")
            existing = session.scalars(
                sa.select(ThreatMitigationRecord).where(
                    ThreatMitigationRecord.org_id == org_id,
                    ThreatMitigationRecord.threat_id == threat_id,
                    ThreatMitigationRecord.mitigation_id == request.mitigation_id,
                )
            ).first()
            if existing is not None:
                stored = _mitigation_from_row(existing)
                if (
                    stored.control_ref != request.control_ref
                    or stored.evidence_ref != request.evidence_ref
                    or stored.note != request.note
                ):
                    raise ValueError(
                        f"mitigation already recorded with different fields: "
                        f"{request.mitigation_id}"
                    )
                return stored
            row = ThreatMitigationRecord(
                mitigation_id=request.mitigation_id,
                org_id=org_id,
                threat_id=threat_id,
                control_ref=request.control_ref,
                evidence_ref=request.evidence_ref,
                note=request.note,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "threats.mitigation_recorded",
                "threat_mitigations",
                f"threat_mitigation:{request.mitigation_id}",
                {
                    "org_id": org_id,
                    "threat_id": threat_id,
                    "control_ref": request.control_ref,
                },
            )
            return _mitigation_from_row(row)

    def list_mitigations(self, org_id: str, threat_id: str | None = None) -> list[ThreatMitigation]:
        with self._sessions() as session:
            stmt = sa.select(ThreatMitigationRecord).where(ThreatMitigationRecord.org_id == org_id)
            if threat_id is not None:
                stmt = stmt.where(ThreatMitigationRecord.threat_id == threat_id)
            rows = session.scalars(
                stmt.order_by(ThreatMitigationRecord.created_at, ThreatMitigationRecord.id)
            ).all()
            return [_mitigation_from_row(r) for r in rows]

    def transition_threat(
        self, threat_id: str, org_id: str, request: ThreatTransitionRequest
    ) -> Threat:
        """Move a threat through IDENTIFIED/MITIGATED/ACCEPTED."""
        with self._sessions.begin() as session:
            row = session.scalars(
                sa.select(ThreatRecord).where(
                    ThreatRecord.threat_id == threat_id,
                    ThreatRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"threat not found: {threat_id}")
            current = ThreatStatus(row.status)
            target = request.status
            if not threat_transition_allowed(current, target):
                raise ValueError(f"illegal threat transition: {current.value} -> {target.value}")
            if current is not target:
                row.status = target.value
                session.flush()
                audit.record(
                    "threats.status_changed",
                    "threats",
                    f"threat:{threat_id}",
                    {
                        "org_id": org_id,
                        "from_status": current.value,
                        "to_status": target.value,
                        "actor_ref": request.actor_ref,
                        "reason_ref": request.reason_ref,
                    },
                )
            return _threat_from_row(row)

    def register_abuse_case(self, request: RegisterAbuseCaseRequest) -> AbuseCase:
        """Idempotent per (org, abuse_case_id); optional threat link."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            if request.threat_id is not None:
                threat = session.scalars(
                    sa.select(ThreatRecord).where(
                        ThreatRecord.org_id == request.org_id,
                        ThreatRecord.threat_id == request.threat_id,
                    )
                ).first()
                if threat is None:
                    raise ValueError(f"threat not found: {request.threat_id}")
            existing = session.scalars(
                sa.select(AbuseCaseRecord).where(
                    AbuseCaseRecord.org_id == request.org_id,
                    AbuseCaseRecord.abuse_case_id == request.abuse_case_id,
                )
            ).first()
            if existing is not None:
                stored = _abuse_from_row(existing)
                if (
                    stored.title != request.title
                    or stored.attack_surface != request.attack_surface
                    or stored.scenario != request.scenario
                    or stored.detection_ref != request.detection_ref
                    or stored.threat_id != request.threat_id
                ):
                    raise ValueError(
                        f"abuse case already registered with different fields: "
                        f"{request.abuse_case_id}"
                    )
                return stored
            row = AbuseCaseRecord(
                abuse_case_id=request.abuse_case_id,
                org_id=request.org_id,
                title=request.title,
                attack_surface=request.attack_surface,
                scenario=request.scenario,
                detection_ref=request.detection_ref,
                threat_id=request.threat_id,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "abuse_cases.registered",
                "abuse_cases",
                f"abuse_case:{request.abuse_case_id}",
                {
                    "org_id": request.org_id,
                    "attack_surface": request.attack_surface,
                    "threat_id": request.threat_id,
                },
            )
            return _abuse_from_row(row)

    def list_abuse_cases(self, org_id: str) -> list[AbuseCase]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(AbuseCaseRecord)
                .where(AbuseCaseRecord.org_id == org_id)
                .order_by(AbuseCaseRecord.created_at, AbuseCaseRecord.id)
            ).all()
            return [_abuse_from_row(r) for r in rows]

    def coverage(self, org_id: str) -> CoverageReport:
        """Pure coverage audit over this org's stored threats."""
        return audit_threat_coverage(
            org_id, self.list_threats(org_id), self.list_mitigations(org_id)
        )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["threats"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ThreatStore:
    return ThreatStore.from_engine(engine)


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


@router.post("/threats", response_model=Threat, name="register_threat")
def register_threat_route(
    request: RegisterThreatRequest,
    store: Annotated[ThreatStore, Depends(_store)],
) -> Threat:
    """Register one threat with its abuse case (idempotent)."""
    try:
        return store.register_threat(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/threats", response_model=list[Threat], name="list_threats")
def list_threats_route(
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    category: Annotated[ThreatCategory | None, Query()] = None,
) -> list[Threat]:
    """List threats for one org, optionally by category."""
    return store.list_threats(org_id, category)


@router.get("/threats/coverage", response_model=CoverageReport, name="threat_coverage")
def threat_coverage_route(
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> CoverageReport:
    """Coverage audit: which threats have evidenced mitigations."""
    return store.coverage(org_id)


@router.get("/threats/{threat_id}", response_model=Threat, name="get_threat")
def get_threat_route(
    threat_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Threat:
    """Get one threat by id and org."""
    stored = store.get_threat(threat_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"threat not found: {threat_id}")
    return stored


@router.post(
    "/threats/{threat_id}/mitigations",
    response_model=ThreatMitigation,
    name="record_threat_mitigation",
)
def record_threat_mitigation_route(
    threat_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: RegisterMitigationRequest,
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> ThreatMitigation:
    """Map one mitigation to a control and its enforcing code."""
    try:
        return store.register_mitigation(threat_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/threats/{threat_id}/transition",
    response_model=Threat,
    name="transition_threat",
)
def transition_threat_route(
    threat_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    request: ThreatTransitionRequest,
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> Threat:
    """Move a threat through its review lifecycle (404/409)."""
    try:
        return store.transition_threat(threat_id, org_id, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post("/abuse-cases", response_model=AbuseCase, name="register_abuse_case")
def register_abuse_case_route(
    request: RegisterAbuseCaseRequest,
    store: Annotated[ThreatStore, Depends(_store)],
) -> AbuseCase:
    """Register one abuse case (idempotent; optional threat link)."""
    try:
        return store.register_abuse_case(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/abuse-cases", response_model=list[AbuseCase], name="list_abuse_cases")
def list_abuse_cases_route(
    store: Annotated[ThreatStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[AbuseCase]:
    """List abuse cases for one org."""
    return store.list_abuse_cases(org_id)
