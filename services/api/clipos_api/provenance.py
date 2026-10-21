"""Provenance manifests and content fingerprints (CLIP-032).

Provenance steps are append-only and stored as an ordered JSON chain on the
manifest.  Manifest hashes are computed over canonical JSON with sorted keys
and never accepted from clients.  Fingerprints are recorded per manifest and
idempotent per ``(manifest_id, algorithm)``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.repository import Base

_DEFAULT_ACTOR = "registry-operator"

_FINGERPRINT_LENGTHS = {"sha256": 64, "sha1": 40, "md5": 32}


class StepType(str, Enum):
    """Spec-constrained provenance step types."""

    CAPTURED = "captured"
    TRANSFORMED = "transformed"
    ENRICHED = "enriched"
    PUBLISHED = "published"


class Algorithm(str, Enum):
    """Recorded fingerprint algorithms; sha256 is preferred."""

    SHA256 = "sha256"
    SHA1 = "sha1"
    MD5 = "md5"


class ProvenanceStep(BaseModel):
    """One ordered provenance step."""

    model_config = ConfigDict(frozen=True)

    step_type: StepType
    actor_ref: str = Field(min_length=1, max_length=512)
    at: datetime
    inputs_refs: list[str] = Field(default_factory=list, max_length=64)
    outputs_ref: str = Field(min_length=1, max_length=512)
    transform_note: str | None = Field(default=None, max_length=2048)

    @field_validator("inputs_refs")
    @classmethod
    def _inputs_are_refs(cls, value: list[str]) -> list[str]:
        for ref in value:
            if not 1 <= len(ref) <= 512:
                raise ValueError("each inputs_refs entry must be 1-512 characters")
        return value


class StepAppendRequest(BaseModel):
    """Payload for appending one provenance step to an item's manifest."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    step: ProvenanceStep


class ProvenanceManifest(BaseModel):
    """One provenance manifest for one source item."""

    model_config = ConfigDict(frozen=True)

    manifest_id: str = Field(min_length=1, max_length=128)
    item_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    chain: list[ProvenanceStep] = Field(default_factory=list)
    manifest_hash: str | None = Field(default=None, min_length=64, max_length=64)
    generated_at: datetime | None = None


class GenerateManifestRequest(BaseModel):
    """Payload for generating (hashing) an item's manifest."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)


class ContentFingerprint(BaseModel):
    """One recorded content fingerprint for one manifest."""

    model_config = ConfigDict(frozen=True)

    fingerprint_id: str = Field(min_length=1, max_length=128)
    manifest_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    algorithm: Algorithm
    fingerprint_hex: str = Field(min_length=32, max_length=64)
    artifact_ref: str = Field(min_length=1, max_length=512)
    computed_at: datetime
    created_at: datetime | None = None

    @field_validator("fingerprint_hex")
    @classmethod
    def _hex_matches_algorithm(cls, value: str, info: Any) -> str:
        algorithm = info.data.get("algorithm")
        if algorithm is not None and len(value) != _FINGERPRINT_LENGTHS[algorithm.value]:
            raise ValueError(
                f"fingerprint_hex must be {_FINGERPRINT_LENGTHS[algorithm.value]} "
                f"hex characters for {algorithm.value}"
            )
        if not all(char in "0123456789abcdefABCDEF" for char in value):
            raise ValueError("fingerprint_hex must contain only hexadecimal characters")
        return value


class FingerprintRecord(Base):
    """SQL row for one recorded content fingerprint."""

    __tablename__ = "content_fingerprints"
    __table_args__ = (
        sa.Index("uq_content_fingerprints_fingerprint_id", "fingerprint_id", unique=True),
        sa.Index(
            "uq_content_fingerprints_manifest_algorithm",
            "manifest_id",
            "algorithm",
            unique=True,
        ),
        sa.Index("ix_content_fingerprints_org_created", "org_id", "created_at"),
        sa.Index("ix_content_fingerprints_manifest", "manifest_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    fingerprint_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    manifest_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    algorithm: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    fingerprint_hex: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    artifact_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    computed_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class ProvenanceManifestRecord(Base):
    """SQL row for one provenance manifest (steps in an append-only JSON chain)."""

    __tablename__ = "provenance_manifests"
    __table_args__ = (
        # Org-scoped: the manifest id is derived from the item id.
        sa.Index("uq_provenance_manifests_manifest_org", "manifest_id", "org_id", unique=True),
        sa.Index("uq_provenance_manifests_item_org", "item_id", "org_id", unique=True),
        sa.Index("ix_provenance_manifests_org_generated", "org_id", "generated_at"),
        sa.Index("ix_provenance_manifests_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    manifest_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    item_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    chain: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False)
    manifest_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    generated_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    created_at: Mapped[Any] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    """Normalize values read from backends that strip the timezone (SQLite)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def canonical_manifest_payload(manifest: ProvenanceManifest) -> dict[str, Any]:
    """Return the canonical JSON-serializable manifest payload used for hashing."""
    return {
        "manifest_id": manifest.manifest_id,
        "item_id": manifest.item_id,
        "org_id": manifest.org_id,
        "chain": [
            {
                "step_type": step.step_type.value,
                "actor_ref": step.actor_ref,
                "at": step.at.isoformat(),
                "inputs_refs": list(step.inputs_refs),
                "outputs_ref": step.outputs_ref,
                "transform_note": step.transform_note,
            }
            for step in manifest.chain
        ],
    }


def canonical_json(payload: Any) -> str:
    """Serialize a payload as canonical JSON with sorted keys."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def manifest_hash(manifest: ProvenanceManifest) -> str:
    """Compute the sha256 manifest hash over canonical JSON."""
    return hashlib.sha256(canonical_json(canonical_manifest_payload(manifest)).encode()).hexdigest()


def _manifest_from_row(row: ProvenanceManifestRecord) -> ProvenanceManifest:
    return ProvenanceManifest(
        manifest_id=row.manifest_id,
        item_id=row.item_id,
        org_id=row.org_id,
        chain=[ProvenanceStep.model_validate(step) for step in row.chain],
        manifest_hash=row.manifest_hash,
        generated_at=_aware(row.generated_at) if row.generated_at is not None else None,
    )


def _fingerprint_from_row(row: FingerprintRecord) -> ContentFingerprint:
    return ContentFingerprint(
        fingerprint_id=row.fingerprint_id,
        manifest_id=row.manifest_id,
        org_id=row.org_id,
        algorithm=Algorithm(row.algorithm),
        fingerprint_hex=row.fingerprint_hex,
        artifact_ref=row.artifact_ref,
        computed_at=_aware(row.computed_at),
        created_at=_aware(row.created_at),
    )


def _audit(action: str, subject: str, details: dict[str, Any]) -> None:
    audit.record(action=action, actor=_DEFAULT_ACTOR, subject=subject, details=details)


class ProvenanceStore:
    """Session-bound SQL persistence for provenance manifests and fingerprints."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> ProvenanceStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    @staticmethod
    def _item(session: Session, item_id: str, org_id: str) -> Any | None:
        """Return the source item of *org_id* — never another org's item."""
        from clipos_api.rights_entities import SourceItemRecord

        return session.scalars(
            sa.select(SourceItemRecord).where(
                SourceItemRecord.item_id == item_id,
                SourceItemRecord.org_id == org_id,
            )
        ).first()

    @staticmethod
    def _manifest(
        session: Session, manifest_id: str, org_id: str | None = None
    ) -> ProvenanceManifestRecord | None:
        query = sa.select(ProvenanceManifestRecord).where(
            ProvenanceManifestRecord.manifest_id == manifest_id
        )
        if org_id is not None:
            query = query.where(ProvenanceManifestRecord.org_id == org_id)
        return session.scalars(query).first()

    def get_manifest(self, manifest_id: str, org_id: str) -> ProvenanceManifest:
        """Return one org-scoped manifest with its steps and hash."""
        with self._sessions() as session:
            row = self._manifest(session, manifest_id, org_id)
            if row is None:
                raise ValueError(f"provenance manifest not found: {manifest_id}")
            return _manifest_from_row(row)

    def list_manifests(self, org_id: str) -> list[ProvenanceManifest]:
        """Return org-scoped manifests ordered by manifest_id."""
        with self._sessions() as session:
            rows = session.scalars(
                sa.select(ProvenanceManifestRecord)
                .where(ProvenanceManifestRecord.org_id == org_id)
                .order_by(ProvenanceManifestRecord.manifest_id)
            ).all()
            return [_manifest_from_row(row) for row in rows]

    def append_step(self, item_id: str, org_id: str, step: ProvenanceStep) -> ProvenanceManifest:
        """Append one step to the item's manifest; updates are rejected by policy."""
        with self._sessions.begin() as session:
            item = self._item(session, item_id, org_id)
            if item is None:
                raise ValueError(f"source item not found: {item_id}")
            row = session.scalars(
                sa.select(ProvenanceManifestRecord).where(
                    ProvenanceManifestRecord.item_id == item_id,
                    ProvenanceManifestRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                row = ProvenanceManifestRecord(
                    manifest_id=f"prov-{item_id}",
                    item_id=item_id,
                    org_id=org_id,
                    chain=[],
                    manifest_hash=None,
                    generated_at=None,
                    created_at=datetime.now(UTC),
                )
                session.add(row)
                session.flush()
            row.chain = [*row.chain, step.model_dump(mode="json")]
            row.manifest_hash = None
            row.generated_at = None
            session.flush()
            stored = _manifest_from_row(row)
        _audit(
            "provenance.step_appended",
            f"provenance_manifest:{stored.manifest_id}",
            {
                "org_id": org_id,
                "item_id": item_id,
                "step_type": step.step_type.value,
                "step_index": len(stored.chain) - 1,
            },
        )
        return stored

    def generate_manifest(self, item_id: str, org_id: str) -> ProvenanceManifest:
        """Compute and record the canonical manifest hash (idempotent for same chain)."""
        with self._sessions.begin() as session:
            item = self._item(session, item_id, org_id)
            if item is None:
                raise ValueError(f"source item not found: {item_id}")
            row = session.scalars(
                sa.select(ProvenanceManifestRecord).where(
                    ProvenanceManifestRecord.item_id == item_id,
                    ProvenanceManifestRecord.org_id == org_id,
                )
            ).first()
            if row is None:
                raise ValueError(f"provenance manifest not found for item: {item_id}")
            generated_at = datetime.now(UTC)
            row.generated_at = generated_at
            session.flush()
            current = _manifest_from_row(row)
            row.manifest_hash = manifest_hash(current)
            session.flush()
            stored = _manifest_from_row(row)
        _audit(
            "provenance.manifest_generated",
            f"provenance_manifest:{stored.manifest_id}",
            {"org_id": org_id, "item_id": item_id, "manifest_hash": stored.manifest_hash},
        )
        return stored

    def register_fingerprint(self, fingerprint: ContentFingerprint) -> ContentFingerprint:
        """Record one fingerprint; idempotent per (manifest_id, algorithm)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            manifest = self._manifest(session, fingerprint.manifest_id)
            if manifest is None:
                raise ValueError(f"provenance manifest not found: {fingerprint.manifest_id}")
            if manifest.org_id != fingerprint.org_id:
                raise ValueError("provenance manifest belongs to a different organization")
            existing = session.scalars(
                sa.select(FingerprintRecord).where(
                    FingerprintRecord.manifest_id == fingerprint.manifest_id,
                    FingerprintRecord.algorithm == fingerprint.algorithm.value,
                )
            ).first()
            if existing is not None:
                stored = _fingerprint_from_row(existing)
                if (
                    stored.fingerprint_hex.lower() != fingerprint.fingerprint_hex.lower()
                    or stored.artifact_ref != fingerprint.artifact_ref
                ):
                    raise ValueError(
                        "fingerprint already registered with different fields: "
                        f"{fingerprint.fingerprint_id}"
                    )
                return stored
            row = FingerprintRecord(
                fingerprint_id=fingerprint.fingerprint_id,
                manifest_id=fingerprint.manifest_id,
                org_id=fingerprint.org_id,
                algorithm=fingerprint.algorithm.value,
                fingerprint_hex=fingerprint.fingerprint_hex.lower(),
                artifact_ref=fingerprint.artifact_ref,
                computed_at=fingerprint.computed_at,
                created_at=now,
            )
            session.add(row)
            session.flush()
            stored = _fingerprint_from_row(row)
        _audit(
            "provenance.fingerprint_registered",
            f"content_fingerprint:{stored.fingerprint_id}",
            {
                "org_id": fingerprint.org_id,
                "manifest_id": fingerprint.manifest_id,
                "algorithm": fingerprint.algorithm.value,
            },
        )
        return stored

    def list_fingerprints(self, manifest_id: str, org_id: str) -> list[ContentFingerprint]:
        """Return org-scoped fingerprints for one manifest."""
        with self._sessions() as session:
            manifest = self._manifest(session, manifest_id)
            if manifest is None or manifest.org_id != org_id:
                raise ValueError(f"provenance manifest not found: {manifest_id}")
            rows = session.scalars(
                sa.select(FingerprintRecord)
                .where(FingerprintRecord.manifest_id == manifest_id)
                .order_by(FingerprintRecord.algorithm)
            ).all()
            return [_fingerprint_from_row(row) for row in rows]


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ProvenanceStore:
    return ProvenanceStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


def append_step_route(
    item_id: str,
    request: StepAppendRequest,
    store: Annotated[ProvenanceStore, Depends(_store)],
) -> ProvenanceManifest:
    """Append one provenance step to an item's manifest."""
    try:
        return store.append_step(item_id, request.org_id, request.step)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def generate_manifest_route(
    item_id: str,
    request: GenerateManifestRequest,
    store: Annotated[ProvenanceStore, Depends(_store)],
) -> ProvenanceManifest:
    """Generate (hash) an item's provenance manifest."""
    try:
        return store.generate_manifest(item_id, request.org_id)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def get_manifest_route(
    manifest_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[ProvenanceStore, Depends(_store)],
) -> ProvenanceManifest:
    """Return one manifest with steps and hash."""
    try:
        return store.get_manifest(manifest_id, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def list_manifests_route(
    store: Annotated[ProvenanceStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1)],
) -> list[ProvenanceManifest]:
    """Return org-scoped manifests."""
    return store.list_manifests(org_id)


def register_fingerprint_route(
    manifest_id: str,
    request: ContentFingerprint,
    store: Annotated[ProvenanceStore, Depends(_store)],
) -> ContentFingerprint:
    """Record one content fingerprint (idempotent per manifest+algorithm)."""
    if request.manifest_id != manifest_id:
        raise HTTPException(status_code=409, detail="manifest_id does not match path")
    try:
        return store.register_fingerprint(request)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message else 409
        raise HTTPException(status_code=status, detail=message) from exc


def list_fingerprints_route(
    manifest_id: str,
    org_id: Annotated[str, Query(min_length=1)],
    store: Annotated[ProvenanceStore, Depends(_store)],
) -> list[ContentFingerprint]:
    """Return org-scoped fingerprints for one manifest."""
    try:
        return store.list_fingerprints(manifest_id, org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


router = APIRouter(tags=["provenance"])
router.add_api_route(
    "/source-items/{item_id}/provenance/steps",
    append_step_route,
    methods=["POST"],
    name="append_provenance_step",
)
router.add_api_route(
    "/source-items/{item_id}/provenance/manifest",
    generate_manifest_route,
    methods=["POST"],
    name="generate_provenance_manifest",
)
router.add_api_route(
    "/source-items/{item_id}/provenance/manifest",
    get_manifest_route,
    methods=["GET"],
    name="get_provenance_manifest",
)
router.add_api_route(
    "/manifests/{manifest_id}/fingerprints",
    register_fingerprint_route,
    methods=["POST"],
    name="register_content_fingerprint",
)
router.add_api_route(
    "/manifests/{manifest_id}/fingerprints",
    list_fingerprints_route,
    methods=["GET"],
    name="list_content_fingerprints",
)
router.add_api_route(
    "/manifests", list_manifests_route, methods=["GET"], name="list_provenance_manifests"
)
