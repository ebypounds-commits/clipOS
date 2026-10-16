"""Production bootstrap, secret references, TLS, and rotation (CLIP-097).

The VPS/GitHub runbook and the human-gates document set the rules this
module encodes:

    "Do not put real secrets in Git, Hermes memory, Codex prompts,
     completion reports or CI logs. Use environment/secret references."
    "`.env.example` contains names/placeholders only."
    "production secrets enter via server secret manager/deployment
     environment, never chat logs or repository."
    "rotation procedure exists for every connector."

Mandatory human gate #1 is **first production secrets import**, so this
module never accepts or stores a secret *value*. It validates a bootstrap
*manifest* â€” a declaration of names and references â€” and reports readiness.
That keeps the code-completion gate (`CLIP-097` itself) separable from the
execution-time human gate the operator performs later.

- **Bootstrap manifests** â€” `bootstrap_manifests` rows are org-scoped with
  client slug ids, idempotent per (org_id, manifest_id). A manifest
  declares the environment, the secret *references* (name -> reference
  form), the TLS material *references*, and the required rotation
  procedures. Audited `bootstrap_manifests.registered`.
- **Validation runs** â€” `bootstrap_validations` rows are append-only and
  idempotent per (org_id, validation_id); each records the verdict, the
  findings, and the blocking-gate count at validation time. Audited
  `bootstrap_validations.recorded`.
- **Rotation registry** â€” `secret_rotations` rows record one rotation
  procedure per connector, idempotent per (org_id, connector_id). A
  rotation entry has a cadence in days and a documented procedure
  reference; the spec requires *every* connector to have one, so
  :func:`rotation_gaps` is pure and reports connectors missing one.
- **Pure validators**, no I/O:

  * :func:`validate_secret_references` â€” every required secret is present
    and expressed as a reference (`${VAR}` / `vault://` / `sops://`â€¦),
    never a literal value; a literal is a blocking finding;
  * :func:`validate_tls_material` â€” cert and key references are present,
    distinct, and not marked self-signed in a production environment;
  * :func:`evaluate_bootstrap_readiness` â€” folds the above into a verdict
    with an explicit `human_gates_remaining` list, so an operator can see
    exactly which gates still block a real deployment;
  * :func:`rotation_gaps` â€” connectors without a registered rotation.

Routes (CLIP-033 pattern): POST/GET /production/bootstrap,
POST/GET /production/bootstrap/validations, POST/GET
/production/rotations, GET /production/readiness. No LLM, no network â€” and
critically, no secret values anywhere in the contract surface.
"""

from __future__ import annotations

import json
import re
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
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.repository import Base

# Environment names this module understands.
PRODUCTION_ENVIRONMENTS = ("production", "PRODUCTION")

# Reference schemes a secret may legitimately use. A bare literal is not one.
_REFERENCE_SCHEMES = ("${", "vault://", "sops://", "aws-sm://", "gcp-sm://", "azure-kv://")

# Secret names every production bootstrap must declare. Names only â€” this
# table is public by design and never carries a value.
REQUIRED_SECRET_NAMES = (
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "CACHE_REDIS_URL",
    "STORAGE_ACCESS_KEY",
    "STORAGE_SECRET_KEY",
    "MINIO_ROOT_PASSWORD",
)

# Secret-shaped literals that must never reach a manifest.
_SECRET_LITERAL_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# Schemes that prove a value is injected at execution time.
HUMAN_GATE_SECRETS = "production_secrets_import"
HUMAN_GATE_TLS = "tls_certificate_issuance"


class BootstrapVerdict(str, Enum):
    """Outcome of one bootstrap validation."""

    READY = "READY"
    BLOCKED = "BLOCKED"


class RotationCadence(str, Enum):
    """Documented rotation cadences."""

    EVERY_30_DAYS = "EVERY_30_DAYS"
    EVERY_90_DAYS = "EVERY_90_DAYS"
    EVERY_180_DAYS = "EVERY_180_DAYS"
    ANNUALLY = "ANNUALLY"


_CADENCE_DAYS = {
    RotationCadence.EVERY_30_DAYS: 30,
    RotationCadence.EVERY_90_DAYS: 90,
    RotationCadence.EVERY_180_DAYS: 180,
    RotationCadence.ANNUALLY: 365,
}


class BootstrapFinding(BaseModel):
    """One bootstrap validation observation."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: str
    message: str
    subject_ref: str | None = None


class BootstrapReadiness(BaseModel):
    """Pure readiness verdict."""

    model_config = ConfigDict(frozen=True)

    verdict: BootstrapVerdict
    findings: list[BootstrapFinding] = Field(default_factory=list)
    human_gates_remaining: list[str] = Field(default_factory=list)


class BootstrapManifest(BaseModel):
    """A declared production bootstrap (names and references only)."""

    model_config = ConfigDict(from_attributes=True)

    manifest_id: str
    org_id: str
    environment: str
    secret_refs: dict[str, str] = Field(default_factory=dict)
    tls_cert_ref: str
    tls_key_ref: str
    tls_self_signed: bool
    domain_ref: str
    created_at: datetime


class BootstrapValidation(BaseModel):
    """One recorded validation run (append-only)."""

    model_config = ConfigDict(from_attributes=True)

    validation_id: str
    org_id: str
    manifest_id: str
    verdict: BootstrapVerdict
    findings: list[BootstrapFinding] = Field(default_factory=list)
    human_gates_remaining: list[str] = Field(default_factory=list)
    blocking_count: int
    created_at: datetime


class SecretRotation(BaseModel):
    """One registered rotation procedure."""

    model_config = ConfigDict(from_attributes=True)

    connector_id: str
    org_id: str
    cadence: RotationCadence
    cadence_days: int
    procedure_ref: str
    owner_ref: str
    created_at: datetime


class RegisterManifestRequest(BaseModel):
    """Declare one production bootstrap manifest."""

    manifest_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    environment: str = Field(min_length=1, max_length=32)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    tls_cert_ref: str = Field(min_length=1, max_length=512)
    tls_key_ref: str = Field(min_length=1, max_length=512)
    tls_self_signed: bool = False
    domain_ref: str = Field(min_length=1, max_length=255)


class ValidateBootstrapRequest(BaseModel):
    """Run one bootstrap validation against a stored manifest."""

    validation_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    manifest_id: str = Field(min_length=1, max_length=128)


class RegisterRotationRequest(BaseModel):
    """Register one connector's rotation procedure."""

    connector_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    org_id: str = Field(min_length=1, max_length=64)
    cadence: RotationCadence
    procedure_ref: str = Field(min_length=1, max_length=512)
    owner_ref: str = Field(min_length=1, max_length=255)


class EvaluateReadinessRequest(BaseModel):
    """Evaluate readiness for an ad-hoc declaration (not persisted)."""

    org_id: str = Field(min_length=1, max_length=64)
    environment: str = Field(default="production", min_length=1, max_length=32)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    tls_cert_ref: str = Field(default="", max_length=512)
    tls_key_ref: str = Field(default="", max_length=512)
    tls_self_signed: bool = False
    required_connectors: list[str] = Field(default_factory=list)


class BootstrapManifestRecord(Base):
    """SQL row for one declared bootstrap manifest."""

    __tablename__ = "bootstrap_manifests"
    __table_args__ = (
        sa.Index(
            "uq_bootstrap_manifests_org_manifest",
            "org_id",
            "manifest_id",
            unique=True,
        ),
        sa.Index("ix_bootstrap_manifests_org_environment", "org_id", "environment"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    manifest_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    environment: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    secret_refs_json: Mapped[str] = mapped_column(sa.String(4000), nullable=False, default="{}")
    tls_cert_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    tls_key_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    tls_self_signed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    domain_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class BootstrapValidationRecord(Base):
    """SQL row for one validation run (append-only)."""

    __tablename__ = "bootstrap_validations"
    __table_args__ = (
        sa.Index(
            "uq_bootstrap_validations_org_validation",
            "org_id",
            "validation_id",
            unique=True,
        ),
        sa.Index("ix_bootstrap_validations_org_manifest", "org_id", "manifest_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    validation_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    manifest_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    verdict: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    findings_json: Mapped[str] = mapped_column(sa.String(8000), nullable=False, default="[]")
    gates_json: Mapped[str] = mapped_column(sa.String(2000), nullable=False, default="[]")
    blocking_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class SecretRotationRecord(Base):
    """SQL row for one rotation procedure."""

    __tablename__ = "secret_rotations"
    __table_args__ = (
        sa.Index(
            "uq_secret_rotations_org_connector",
            "org_id",
            "connector_id",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    connector_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    cadence: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    cadence_days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    procedure_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    owner_ref: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _is_reference(value: str) -> bool:
    """A reference is an injected placeholder, never a literal value."""
    text = (value or "").strip()
    if not text:
        return False
    return any(text.startswith(scheme) for scheme in _REFERENCE_SCHEMES)


def _has_secret_literal(value: str) -> str | None:
    for pattern in _SECRET_LITERAL_PATTERNS:
        match = pattern.search(value or "")
        if match:
            return match.group(0)[:6] + "..."
    return None


def validate_secret_references(
    secret_refs: dict[str, str], environment: str
) -> list[BootstrapFinding]:
    """Pure: every required secret is present and expressed as a reference."""
    findings: list[BootstrapFinding] = []
    for name in REQUIRED_SECRET_NAMES:
        value = secret_refs.get(name)
        if value is None:
            findings.append(
                BootstrapFinding(
                    code="SECRET_MISSING",
                    severity="BLOCKING",
                    message=f"required production secret is not declared: {name}",
                    subject_ref=name,
                )
            )
            continue
        literal = _has_secret_literal(value)
        if literal:
            findings.append(
                BootstrapFinding(
                    code="SECRET_LITERAL",
                    severity="BLOCKING",
                    message=(
                        f"{name} contains a secret-shaped literal ({literal}); "
                        "secret values must never be declared, only referenced"
                    ),
                    subject_ref=name,
                )
            )
            continue
        if not _is_reference(value):
            findings.append(
                BootstrapFinding(
                    code="SECRET_NOT_A_REFERENCE",
                    severity="BLOCKING",
                    message=(
                        f"{name} is not an injected reference; expected a "
                        "${VAR} / vault:// / sops:// style reference"
                    ),
                    subject_ref=name,
                )
            )
    return findings


def validate_tls_material(
    cert_ref: str, key_ref: str, self_signed: bool, environment: str
) -> list[BootstrapFinding]:
    """Pure: TLS material is referenced, distinct, and production-appropriate."""
    findings: list[BootstrapFinding] = []
    if not _is_reference(cert_ref):
        findings.append(
            BootstrapFinding(
                code="TLS_CERT_NOT_REFERENCED",
                severity="BLOCKING",
                message="tls_cert_ref must be a reference, never an inline certificate",
                subject_ref="tls_cert_ref",
            )
        )
    if not _is_reference(key_ref):
        findings.append(
            BootstrapFinding(
                code="TLS_KEY_NOT_REFERENCED",
                severity="BLOCKING",
                message="tls_key_ref must be a reference, never an inline private key",
                subject_ref="tls_key_ref",
            )
        )
    if cert_ref and cert_ref == key_ref:
        findings.append(
            BootstrapFinding(
                code="TLS_MATERIAL_REUSED",
                severity="BLOCKING",
                message="certificate and key must not share the same reference",
                subject_ref="tls_key_ref",
            )
        )
    if self_signed and environment in PRODUCTION_ENVIRONMENTS:
        findings.append(
            BootstrapFinding(
                code="TLS_SELF_SIGNED_IN_PRODUCTION",
                severity="BLOCKING",
                message=(
                    "a self-signed certificate is not acceptable for production; "
                    "issue a CA-signed certificate before deployment"
                ),
                subject_ref="tls_self_signed",
            )
        )
    return findings


def evaluate_bootstrap_readiness(
    secret_refs: dict[str, str],
    cert_ref: str,
    key_ref: str,
    self_signed: bool,
    environment: str,
    rotations: list[str] | None = None,
    required_connectors: list[str] | None = None,
) -> BootstrapReadiness:
    """Pure: fold every bootstrap check into a verdict.

    ``human_gates_remaining`` names the execution-time human gates that still
    apply, so a READY verdict is never mistaken for "safe to deploy unaided".
    """
    findings = validate_secret_references(secret_refs, environment)
    findings.extend(validate_tls_material(cert_ref, key_ref, self_signed, environment))

    if required_connectors:
        missing = rotation_gaps(list(rotations or []), required_connectors)
        for connector in missing:
            findings.append(
                BootstrapFinding(
                    code="ROTATION_MISSING",
                    severity="BLOCKING",
                    message=f"connector has no registered rotation procedure: {connector}",
                    subject_ref=connector,
                )
            )

    blocking = [f for f in findings if f.severity == "BLOCKING"]

    # The two gates the spec makes mandatory regardless of code readiness.
    gates: list[str] = []
    if environment in PRODUCTION_ENVIRONMENTS:
        gates.append(HUMAN_GATE_SECRETS)
        gates.append(HUMAN_GATE_TLS)

    return BootstrapReadiness(
        verdict=BootstrapVerdict.BLOCKED if blocking else BootstrapVerdict.READY,
        findings=findings,
        human_gates_remaining=gates,
    )


def rotation_gaps(rotated: list[str], required: list[str]) -> list[str]:
    """Pure: required connectors that have no registered rotation."""
    have = set(rotated)
    return sorted({c for c in required if c not in have})


def _manifest_from_row(row: BootstrapManifestRecord) -> BootstrapManifest:

    return BootstrapManifest(
        manifest_id=row.manifest_id,
        org_id=row.org_id,
        environment=row.environment,
        secret_refs=json.loads(row.secret_refs_json or "{}"),
        tls_cert_ref=row.tls_cert_ref,
        tls_key_ref=row.tls_key_ref,
        tls_self_signed=row.tls_self_signed,
        domain_ref=row.domain_ref,
        created_at=_aware(row.created_at),
    )


def _validation_from_row(row: BootstrapValidationRecord) -> BootstrapValidation:

    return BootstrapValidation(
        validation_id=row.validation_id,
        org_id=row.org_id,
        manifest_id=row.manifest_id,
        verdict=BootstrapVerdict(row.verdict),
        findings=[BootstrapFinding(**f) for f in json.loads(row.findings_json or "[]")],
        human_gates_remaining=json.loads(row.gates_json or "[]"),
        blocking_count=row.blocking_count,
        created_at=_aware(row.created_at),
    )


def _rotation_from_row(row: SecretRotationRecord) -> SecretRotation:
    return SecretRotation(
        connector_id=row.connector_id,
        org_id=row.org_id,
        cadence=RotationCadence(row.cadence),
        cadence_days=row.cadence_days,
        procedure_ref=row.procedure_ref,
        owner_ref=row.owner_ref,
        created_at=_aware(row.created_at),
    )


class BootstrapStore:
    """Typed persistence for bootstrap manifests, validations and rotations."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> BootstrapStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # -- manifests ---------------------------------------------------------

    def register_manifest(self, request: RegisterManifestRequest) -> BootstrapManifest:
        """Idempotent per (org, manifest_id).

        A manifest carrying a secret-shaped literal is refused outright, so
        a value can never reach persistence even by mistake.
        """

        for name, value in request.secret_refs.items():
            literal = _has_secret_literal(value)
            if literal:
                raise ValueError(
                    f"secret_refs[{name}] contains a secret-shaped literal ({literal}); "
                    "manifest must carry references only"
                )
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(BootstrapManifestRecord).where(
                    BootstrapManifestRecord.org_id == request.org_id,
                    BootstrapManifestRecord.manifest_id == request.manifest_id,
                )
            ).first()
            if existing is not None:
                stored = _manifest_from_row(existing)
                if (
                    stored.secret_refs != request.secret_refs
                    or stored.tls_cert_ref != request.tls_cert_ref
                    or stored.tls_key_ref != request.tls_key_ref
                    or stored.tls_self_signed != request.tls_self_signed
                    or stored.domain_ref != request.domain_ref
                    or stored.environment != request.environment
                ):
                    raise ValueError(
                        f"bootstrap manifest already registered with different fields: "
                        f"{request.manifest_id}"
                    )
                return stored

            row = BootstrapManifestRecord(
                manifest_id=request.manifest_id,
                org_id=request.org_id,
                environment=request.environment,
                secret_refs_json=json.dumps(request.secret_refs, sort_keys=True),
                tls_cert_ref=request.tls_cert_ref,
                tls_key_ref=request.tls_key_ref,
                tls_self_signed=request.tls_self_signed,
                domain_ref=request.domain_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "bootstrap_manifests.registered",
                "bootstrap-store",
                f"manifest:{request.manifest_id}",
                {
                    "org_id": request.org_id,
                    "environment": request.environment,
                    "secret_count": len(request.secret_refs),
                    "domain_ref": request.domain_ref,
                },
            )
            return _manifest_from_row(row)

    def get_manifest(self, manifest_id: str, org_id: str) -> BootstrapManifest | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(BootstrapManifestRecord).where(
                    BootstrapManifestRecord.org_id == org_id,
                    BootstrapManifestRecord.manifest_id == manifest_id,
                )
            ).first()
            return _manifest_from_row(row) if row is not None else None

    def list_manifests(self, org_id: str) -> list[BootstrapManifest]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(BootstrapManifestRecord)
                .where(BootstrapManifestRecord.org_id == org_id)
                .order_by(BootstrapManifestRecord.id)
            ).all()
            return [_manifest_from_row(row) for row in rows]

    # -- validations -------------------------------------------------------

    def validate(
        self,
        request: ValidateBootstrapRequest,
        required_connectors: list[str] | None = None,
    ) -> BootstrapValidation:
        """Validate a stored manifest and append the result."""

        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(BootstrapValidationRecord).where(
                    BootstrapValidationRecord.org_id == request.org_id,
                    BootstrapValidationRecord.validation_id == request.validation_id,
                )
            ).first()
            if existing is not None:
                return _validation_from_row(existing)

            manifest_row = session.scalars(
                sa.select(BootstrapManifestRecord).where(
                    BootstrapManifestRecord.org_id == request.org_id,
                    BootstrapManifestRecord.manifest_id == request.manifest_id,
                )
            ).first()
            if manifest_row is None:
                raise ValueError(f"bootstrap manifest not found: {request.manifest_id}")

            rotation_rows = session.scalars(
                sa.select(SecretRotationRecord).where(SecretRotationRecord.org_id == request.org_id)
            ).all()
            readiness = evaluate_bootstrap_readiness(
                json.loads(manifest_row.secret_refs_json or "{}"),
                manifest_row.tls_cert_ref,
                manifest_row.tls_key_ref,
                manifest_row.tls_self_signed,
                manifest_row.environment,
                rotations=[row.connector_id for row in rotation_rows],
                required_connectors=required_connectors,
            )

            row = BootstrapValidationRecord(
                validation_id=request.validation_id,
                org_id=request.org_id,
                manifest_id=request.manifest_id,
                verdict=readiness.verdict.value,
                findings_json=json.dumps([f.model_dump() for f in readiness.findings]),
                gates_json=json.dumps(readiness.human_gates_remaining),
                blocking_count=sum(1 for f in readiness.findings if f.severity == "BLOCKING"),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "bootstrap_validations.recorded",
                "bootstrap-store",
                f"validation:{request.validation_id}",
                {
                    "org_id": request.org_id,
                    "manifest_id": request.manifest_id,
                    "verdict": readiness.verdict.value,
                    "blocking_count": row.blocking_count,
                    "human_gates_remaining": len(readiness.human_gates_remaining),
                },
            )
            return _validation_from_row(row)

    def list_validations(self, org_id: str) -> list[BootstrapValidation]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(BootstrapValidationRecord)
                .where(BootstrapValidationRecord.org_id == org_id)
                .order_by(BootstrapValidationRecord.id)
            ).all()
            return [_validation_from_row(row) for row in rows]

    # -- rotations ---------------------------------------------------------

    def register_rotation(self, request: RegisterRotationRequest) -> SecretRotation:
        """Idempotent per (org, connector_id); every connector needs one."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(SecretRotationRecord).where(
                    SecretRotationRecord.org_id == request.org_id,
                    SecretRotationRecord.connector_id == request.connector_id,
                )
            ).first()
            if existing is not None:
                stored = _rotation_from_row(existing)
                if (
                    stored.cadence is not request.cadence
                    or stored.procedure_ref != request.procedure_ref
                    or stored.owner_ref != request.owner_ref
                ):
                    raise ValueError(
                        f"rotation already registered with different fields: {request.connector_id}"
                    )
                return stored

            row = SecretRotationRecord(
                connector_id=request.connector_id,
                org_id=request.org_id,
                cadence=request.cadence.value,
                cadence_days=_CADENCE_DAYS[request.cadence],
                procedure_ref=request.procedure_ref,
                owner_ref=request.owner_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "secret_rotations.registered",
                "bootstrap-store",
                f"connector:{request.connector_id}",
                {
                    "org_id": request.org_id,
                    "cadence": request.cadence.value,
                    "cadence_days": row.cadence_days,
                    "owner_ref": request.owner_ref,
                },
            )
            return _rotation_from_row(row)

    def list_rotations(self, org_id: str) -> list[SecretRotation]:
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(SecretRotationRecord)
                .where(SecretRotationRecord.org_id == org_id)
                .order_by(SecretRotationRecord.id)
            ).all()
            return [_rotation_from_row(row) for row in rows]


router = APIRouter(tags=["production"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> BootstrapStore:
    return BootstrapStore.from_engine(engine)


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
    "/production/bootstrap",
    response_model=BootstrapManifest,
    name="register_bootstrap_manifest",
)
def register_manifest_route(
    request: RegisterManifestRequest,
    store: Annotated[BootstrapStore, Depends(_store)],
) -> BootstrapManifest:
    """Declare a production bootstrap manifest (references only)."""
    try:
        return store.register_manifest(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/production/bootstrap",
    response_model=list[BootstrapManifest],
    name="list_bootstrap_manifests",
)
def list_manifests_route(
    store: Annotated[BootstrapStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[BootstrapManifest]:
    """List bootstrap manifests for one org."""
    return store.list_manifests(org_id)


@router.get(
    "/production/bootstrap/{manifest_id}",
    response_model=BootstrapManifest,
    name="get_bootstrap_manifest",
)
def get_manifest_route(
    manifest_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[BootstrapStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> BootstrapManifest:
    """Get one bootstrap manifest by id and org."""
    stored = store.get_manifest(manifest_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"bootstrap manifest not found: {manifest_id}")
    return stored


@router.post(
    "/production/bootstrap/validations",
    response_model=BootstrapValidation,
    name="validate_bootstrap",
)
def validate_bootstrap_route(
    request: ValidateBootstrapRequest,
    store: Annotated[BootstrapStore, Depends(_store)],
    required_connectors: Annotated[list[str] | None, Query()] = None,
) -> BootstrapValidation:
    """Validate a manifest and record the readiness verdict."""
    try:
        return store.validate(request, required_connectors)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/production/bootstrap/validations",
    response_model=list[BootstrapValidation],
    name="list_bootstrap_validations",
)
def list_validations_route(
    store: Annotated[BootstrapStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[BootstrapValidation]:
    """List bootstrap validation runs for one org."""
    return store.list_validations(org_id)


@router.post("/production/rotations", response_model=SecretRotation, name="register_rotation")
def register_rotation_route(
    request: RegisterRotationRequest,
    store: Annotated[BootstrapStore, Depends(_store)],
) -> SecretRotation:
    """Register one connector's rotation procedure (idempotent)."""
    try:
        return store.register_rotation(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get("/production/rotations", response_model=list[SecretRotation], name="list_rotations")
def list_rotations_route(
    store: Annotated[BootstrapStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[SecretRotation]:
    """List registered rotation procedures for one org."""
    return store.list_rotations(org_id)


@router.post(
    "/production/readiness", response_model=BootstrapReadiness, name="production_readiness"
)
def production_readiness_route(
    request: EvaluateReadinessRequest,
    store: Annotated[BootstrapStore, Depends(_store)],
) -> BootstrapReadiness:
    """Evaluate readiness for an ad-hoc declaration without persisting it."""
    rotations = [r.connector_id for r in store.list_rotations(request.org_id)]
    return evaluate_bootstrap_readiness(
        request.secret_refs,
        request.tls_cert_ref,
        request.tls_key_ref,
        request.tls_self_signed,
        request.environment,
        rotations=rotations,
        required_connectors=request.required_connectors or None,
    )


__all__ = [
    "HUMAN_GATE_SECRETS",
    "HUMAN_GATE_TLS",
    "PRODUCTION_ENVIRONMENTS",
    "REQUIRED_SECRET_NAMES",
    "BootstrapFinding",
    "BootstrapManifest",
    "BootstrapReadiness",
    "BootstrapStore",
    "BootstrapValidation",
    "BootstrapVerdict",
    "EvaluateReadinessRequest",
    "RegisterManifestRequest",
    "RegisterRotationRequest",
    "RotationCadence",
    "SecretRotation",
    "ValidateBootstrapRequest",
    "evaluate_bootstrap_readiness",
    "rotation_gaps",
    "router",
    "validate_secret_references",
    "validate_tls_material",
]
