"""Generated-media provenance and AI-label metadata (CLIP-057).

Closes M13 with the compliance metadata layer for everything CLIP-054/055
generate:

- **AI labels** (`ai_labels`, migration 0037) — every generated asset
  carries a disclosure row: which generator alias (Model Registry ref,
  never defaulted, never a vendor id) produced it, at which asset kind
  (reuse M7 `MediaAssetKind` AUDIO/IMAGE/VIDEO), with the label value
  (`AI_GENERATED` for all current synthetic output; `AI_ASSISTED` left
  available for tool-assisted edits). Idempotent per
  `(org_id, label_id)`; changed asset/generator/label conflicts (409).
- **Transformation provenance** — for brief-linked generations (image/
  video from CLIP-055) the helper also appends a CLIP-049
  `transformation_records` row (`from_ref` = deterministic
  `gen-prompt:{sha256[:16]}` prompt reference, `to_ref` = workdir-relative
  output, `method` = image-generation/video-generation) so the 07 rights
  chain covers synthetic outputs with the same rows used for edits.
  Voice synthesis (CLIP-054) is not brief-linked, so it labels only.

`record_generated_media(...)` is the single registration helper used by
the generation routes (auto-labeling at HTTP generation time) **and** by
the explicit `POST /generated-media-metadata` endpoint for workflow-side
generation — both idempotent end-to-end via deterministic label and
transformation ids (`{org}:ai-label:{sha1[:16]}` /
`{org}:gen:{sha1[:16]}`).

Audits: `ai_labels.recorded` (transformation rows audit through the
CLIP-049 store). Routes (CLIP-033 pattern): `POST
/generated-media-metadata`, `GET /ai-labels`, `GET /ai-labels/{label_id}`
(404). No LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from hashlib import sha1
from pathlib import Path
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.content_evals import ContentEvalStore, TransformationRecord
from clipos_api.media_ingestion import MediaAssetKind
from clipos_api.render_pipeline import render_workdir
from clipos_api.repository import Base


class LabelValue(str, Enum):
    """Disclosure value recorded on one AI label."""

    AI_GENERATED = "AI_GENERATED"
    AI_ASSISTED = "AI_ASSISTED"


class AiLabel(BaseModel):
    """One AI-label disclosure row; natural key = (org_id, label_id)."""

    model_config = ConfigDict(frozen=True)

    label_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    asset_kind: MediaAssetKind
    asset_ref: str = Field(min_length=1, max_length=512)
    generator_alias: str = Field(min_length=1, max_length=128)
    label: LabelValue = LabelValue.AI_GENERATED
    detail_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None


class GeneratedMediaMetadataRequest(BaseModel):
    """Request body for explicit generated-media metadata registration."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str | None = Field(default=None, min_length=1, max_length=128)
    asset_kind: MediaAssetKind
    asset_ref: str = Field(min_length=1, max_length=512)
    generator_alias: str = Field(min_length=1, max_length=128)
    prompt: str | None = Field(default=None, max_length=4000)
    label: LabelValue = LabelValue.AI_GENERATED
    detail_ref: str | None = Field(default=None, max_length=512)


class AiLabelRecord(Base):
    """SQL row for one AI label."""

    __tablename__ = "ai_labels"
    __table_args__ = (
        sa.Index("uq_ai_labels_org_label", "org_id", "label_id", unique=True),
        sa.Index("ix_ai_labels_org_kind", "org_id", "asset_kind"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    label_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    asset_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    asset_ref: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    generator_alias: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    label: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    detail_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _label_from_row(row: AiLabelRecord) -> AiLabel:
    return AiLabel(
        label_id=row.label_id,
        org_id=row.org_id,
        asset_kind=MediaAssetKind(row.asset_kind),
        asset_ref=row.asset_ref,
        generator_alias=row.generator_alias,
        label=LabelValue(row.label),
        detail_ref=row.detail_ref,
        created_at=_aware(row.created_at),
    )


def deterministic_label_id(
    org_id: str, asset_kind: str, asset_ref: str, generator_alias: str
) -> str:
    """Deterministic label id for auto-labeling (pure, idempotent)."""
    digest = sha1(f"{asset_kind}:{asset_ref}:{generator_alias}".encode()).hexdigest()[:16]
    return f"{org_id}:ai-label:{digest}"[:128]


def deterministic_transformation_id(
    org_id: str, brief_id: str, asset_kind: str, asset_ref: str
) -> str:
    """Deterministic transformation id for generation provenance (pure)."""
    digest = sha1(f"{brief_id}:{asset_kind}:{asset_ref}".encode()).hexdigest()[:16]
    return f"{org_id}:gen:{digest}"[:128]


def relative_ref(path: str) -> str:
    """Store a filesystem path workdir-relative when possible (pure-ish)."""
    try:
        return str(Path(path).resolve().relative_to(render_workdir()))
    except (ValueError, OSError):
        return path


class GeneratedMediaStore:
    """Typed persistence for AI labels."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> GeneratedMediaStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_label(self, label: AiLabel) -> AiLabel:
        """Idempotent register per (org, label_id); conflict on change."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            existing = session.scalars(
                sa.select(AiLabelRecord).where(
                    AiLabelRecord.org_id == label.org_id,
                    AiLabelRecord.label_id == label.label_id,
                )
            ).first()
            if existing is not None:
                stored = _label_from_row(existing)
                if (
                    stored.asset_kind != label.asset_kind
                    or stored.asset_ref != label.asset_ref
                    or stored.generator_alias != label.generator_alias
                    or stored.label != label.label
                    or stored.detail_ref != label.detail_ref
                ):
                    raise ValueError(
                        f"ai label already registered with different fields: {label.label_id}"
                    )
                return stored
            row = AiLabelRecord(
                label_id=label.label_id,
                org_id=label.org_id,
                asset_kind=label.asset_kind.value,
                asset_ref=label.asset_ref,
                generator_alias=label.generator_alias,
                label=label.label.value,
                detail_ref=label.detail_ref,
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "ai_labels.recorded",
                "ai_labels",
                f"ai_label:{label.label_id}",
                {
                    "org_id": label.org_id,
                    "asset_kind": label.asset_kind.value,
                    "generator_alias": label.generator_alias,
                    "label": label.label.value,
                },
            )
            return _label_from_row(row)

    def get_label(self, label_id: str, org_id: str) -> AiLabel | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(AiLabelRecord).where(
                    AiLabelRecord.label_id == label_id,
                    AiLabelRecord.org_id == org_id,
                )
            ).first()
            return _label_from_row(row) if row is not None else None

    def list_labels(self, org_id: str, asset_kind: MediaAssetKind | None = None) -> list[AiLabel]:
        with self._sessions() as session:
            stmt = sa.select(AiLabelRecord).where(AiLabelRecord.org_id == org_id)
            if asset_kind is not None:
                stmt = stmt.where(AiLabelRecord.asset_kind == asset_kind.value)
            rows = session.scalars(stmt.order_by(AiLabelRecord.created_at, AiLabelRecord.id)).all()
            return [_label_from_row(row) for row in rows]


def record_generated_media(
    label_store: GeneratedMediaStore,
    request: GeneratedMediaMetadataRequest,
    *,
    transformation_store: ContentEvalStore | None = None,
) -> AiLabel:
    """Register AI-label (+ optional transformation provenance) idempotently.

    Always registers the label under a deterministic id. When both
    ``brief_id`` and ``transformation_store`` are provided, also appends
    the CLIP-049 transformation row (from prompt ref -> to asset ref,
    method derived from the asset kind) so image/video generation enters
    the 07 rights chain. Returns the persisted label.
    """
    label_id = deterministic_label_id(
        request.org_id,
        request.asset_kind.value,
        request.asset_ref,
        request.generator_alias,
    )
    label = AiLabel(
        label_id=label_id,
        org_id=request.org_id,
        asset_kind=request.asset_kind,
        asset_ref=request.asset_ref,
        generator_alias=request.generator_alias,
        label=request.label,
        detail_ref=request.detail_ref,
    )
    if request.brief_id is not None:
        # validate the brief BEFORE persisting anything (no partial state)
        from clipos_api.content_planning import ContentBriefRecord

        with label_store._sessions() as session:
            brief = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == request.brief_id,
                    ContentBriefRecord.org_id == request.org_id,
                )
            ).first()
        if brief is None:
            raise ValueError(f"content brief not found: {request.brief_id}")

    stored = label_store.register_label(label)

    if request.brief_id is not None and transformation_store is not None:
        method = f"{request.asset_kind.value.lower()}-generation"
        prompt_ref = (
            f"gen-prompt:{sha1(request.prompt.encode()).hexdigest()[:16]}"
            if request.prompt
            else f"gen-prompt:{label_id}"
        )
        transformation_store.register_transformation(
            TransformationRecord(
                transformation_id=deterministic_transformation_id(
                    request.org_id,
                    request.brief_id,
                    request.asset_kind.value,
                    request.asset_ref,
                ),
                org_id=request.org_id,
                brief_id=request.brief_id,
                from_ref=prompt_ref,
                to_ref=request.asset_ref,
                method=method,
                description=(request.prompt or "")[:2048] or None,
            )
        )
    return stored


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["ai-labels"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> GeneratedMediaStore:
    return GeneratedMediaStore.from_engine(engine)


def _transformation_store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> ContentEvalStore:
    return ContentEvalStore.from_engine(engine)


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
    "/generated-media-metadata",
    response_model=AiLabel,
    name="register_generated_media_metadata",
)
def register_generated_media_metadata_route(
    request: GeneratedMediaMetadataRequest,
    store: Annotated[GeneratedMediaStore, Depends(_store)],
    transformations: Annotated[ContentEvalStore, Depends(_transformation_store)],
) -> AiLabel:
    """Register AI-label (+ transformation provenance when brief-linked)."""
    try:
        return record_generated_media(store, request, transformation_store=transformations)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/ai-labels",
    response_model=list[AiLabel],
    name="list_ai_labels",
)
def list_ai_labels_route(
    store: Annotated[GeneratedMediaStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    asset_kind: Annotated[MediaAssetKind | None, Query()] = None,
) -> list[AiLabel]:
    """List AI labels for one org, optionally filtered by asset kind."""
    return store.list_labels(org_id, asset_kind)


@router.get(
    "/ai-labels/{label_id}",
    response_model=AiLabel,
    name="get_ai_label",
)
def get_ai_label_route(
    label_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[GeneratedMediaStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> AiLabel:
    """Get one AI label by id and org."""
    stored = store.get_label(label_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"ai label not found: {label_id}")
    return stored
