"""Render project DSL and validation (CLIP-050).

The declarative document that drives the M12 media studio: a render
project pins one content brief to a **validated** render spec — inputs
(candidate clip + optional asset refs), a composition (aspect variant,
duration window, optional captions and audio normalization), and a
non-empty list of output platform variants. `validate_render_spec` is the
DSL's pure validator: structural checks, allowed aspect ratios, duration
bounds (1s..10min), LUFS target bounds, duplicate output platforms, and
unknown top-level keys — all deterministic, no FFmpeg, no network.

Registration is idempotent per `(org_id, render_project_id)`, gates on an
existing brief (404), rejects invalid specs (422 with the validator's
error list), and conflicts on changed fields (409). `POST
/render-specs/validate` dry-runs the validator without persistence so
tooling can pre-check documents. Audits: `render_projects.registered`.
The spec JSON stored here is the source document CLIP-051's render
pipeline will consume. No LLM, no provider identifiers (platform entries
are generic slugs).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.content_planning import ContentBriefRecord
from clipos_api.repository import Base

#: Aspect ratios accepted by the studio (M12 aspect variants).
ALLOWED_ASPECTS = frozenset({"9:16", "16:9", "1:1"})

#: Top-level keys the spec document may contain.
ALLOWED_SPEC_KEYS = frozenset({"version", "inputs", "composition", "outputs"})

#: Duration bounds for one render window (1 second .. 10 minutes).
MIN_DURATION_MS = 1000
MAX_DURATION_MS = 600_000

#: Loudness normalization target bounds (LUFS).
MIN_TARGET_LUFS = -24.0
MAX_TARGET_LUFS = -5.0

MAX_OUTPUTS = 8


def validate_render_spec(spec: dict[str, Any]) -> list[str]:
    """Pure DSL validator: returns a list of errors (empty = valid).

    Checks structure, aspect set, duration bounds, captions/audio shapes,
    output platform uniqueness, and unknown top-level keys. Deterministic
    and side-effect free — the single source of truth for spec validity.
    """
    errors: list[str] = []

    for key in spec:
        if key not in ALLOWED_SPEC_KEYS:
            errors.append(f"unknown top-level key: {key}")

    version = spec.get("version")
    if version != 1:
        errors.append("version must be 1")

    inputs = spec.get("inputs")
    if not isinstance(inputs, dict):
        errors.append("inputs must be an object")
    else:
        candidate_id = inputs.get("candidate_id")
        if not isinstance(candidate_id, str) or not (1 <= len(candidate_id) <= 128):
            errors.append("inputs.candidate_id must be a string of 1..128 chars")

    composition = spec.get("composition")
    if not isinstance(composition, dict):
        errors.append("composition must be an object")
    else:
        aspect = composition.get("aspect")
        if aspect not in ALLOWED_ASPECTS:
            errors.append(f"composition.aspect must be one of {sorted(ALLOWED_ASPECTS)}")
        duration = composition.get("duration_ms")
        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or not (MIN_DURATION_MS <= duration <= MAX_DURATION_MS)
        ):
            errors.append(
                f"composition.duration_ms must be an int in {MIN_DURATION_MS}..{MAX_DURATION_MS}"
            )
        captions = composition.get("captions")
        if captions is not None:
            if not isinstance(captions, dict):
                errors.append("composition.captions must be an object")
            else:
                if not isinstance(captions.get("enabled"), bool):
                    errors.append("composition.captions.enabled must be a boolean")
                style_ref = captions.get("style_ref")
                if style_ref is not None and not (
                    isinstance(style_ref, str) and 1 <= len(style_ref) <= 128
                ):
                    errors.append(
                        "composition.captions.style_ref must be null or a string of 1..128 chars"
                    )
        audio = composition.get("audio_normalization")
        if audio is not None:
            if not isinstance(audio, dict):
                errors.append("composition.audio_normalization must be an object")
            else:
                if not isinstance(audio.get("enabled"), bool):
                    errors.append("composition.audio_normalization.enabled must be a boolean")
                target = audio.get("target_lufs")
                if audio.get("enabled") is True:
                    if not isinstance(target, (int, float)) or isinstance(target, bool):
                        errors.append(
                            "composition.audio_normalization.target_lufs is required when enabled"
                        )
                    elif not (MIN_TARGET_LUFS <= float(target) <= MAX_TARGET_LUFS):
                        errors.append(
                            f"composition.audio_normalization.target_lufs must be in "
                            f"{MIN_TARGET_LUFS}..{MAX_TARGET_LUFS}"
                        )

    outputs = spec.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        errors.append("outputs must be a non-empty array")
    elif len(outputs) > MAX_OUTPUTS:
        errors.append(f"outputs must contain at most {MAX_OUTPUTS} entries")
    else:
        seen_platforms: set[str] = set()
        for index, output in enumerate(outputs):
            if not isinstance(output, dict):
                errors.append(f"outputs[{index}] must be an object")
                continue
            platform = output.get("platform")
            if not isinstance(platform, str) or not (1 <= len(platform) <= 64):
                errors.append(f"outputs[{index}].platform must be a string of 1..64 chars")
                continue
            if platform in seen_platforms:
                errors.append(f"duplicate output platform: {platform}")
            seen_platforms.add(platform)

    return errors


class RenderProject(BaseModel):
    """One render project; natural key = (org_id, render_project_id)."""

    model_config = ConfigDict(frozen=True)

    render_project_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    brief_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=255)
    spec: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RenderSpecValidateRequest(BaseModel):
    """Request body for a dry-run spec validation (no persistence)."""

    model_config = ConfigDict(frozen=True)

    spec: dict[str, Any]


class ValidationResult(BaseModel):
    """Outcome of one dry-run spec validation."""

    model_config = ConfigDict(frozen=True)

    valid: bool
    errors: list[str] = Field(default_factory=list)


class RenderProjectRecord(Base):
    """SQL row for one render project (validated DSL document)."""

    __tablename__ = "render_projects"
    __table_args__ = (
        sa.Index(
            "uq_render_projects_org_render",
            "org_id",
            "render_project_id",
            unique=True,
        ),
        sa.Index("ix_render_projects_org_brief", "org_id", "brief_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    render_project_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    brief_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(
        sa.JSON, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _project_from_row(row: RenderProjectRecord) -> RenderProject:
    return RenderProject(
        render_project_id=row.render_project_id,
        org_id=row.org_id,
        brief_id=row.brief_id,
        name=row.name,
        spec=dict(row.spec or {}),
        created_at=_aware(row.created_at),
    )


class RenderProjectStore:
    """Typed persistence for render projects (validated specs)."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RenderProjectStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def register_project(self, project: RenderProject) -> RenderProject:
        """Validate the spec, gate on the brief, idempotent per (org, id).

        Invalid specs raise before persistence (route maps to 422 with the
        validator errors); unknown brief -> 404; identical re-register
        returns the stored row; changed fields -> conflict.
        """
        now = datetime.now(UTC)
        errors = validate_render_spec(project.spec)
        if errors:
            raise ValueError("invalid render spec: " + "; ".join(errors[:5]))
        with self._sessions.begin() as session:
            brief = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == project.brief_id,
                    ContentBriefRecord.org_id == project.org_id,
                )
            ).first()
            if brief is None:
                raise ValueError(f"content brief not found: {project.brief_id}")
            existing = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.org_id == project.org_id,
                    RenderProjectRecord.render_project_id == project.render_project_id,
                )
            ).first()
            if existing is not None:
                stored = _project_from_row(existing)
                if (
                    stored.brief_id != project.brief_id
                    or stored.name != project.name
                    or stored.spec != project.spec
                ):
                    raise ValueError(
                        f"render project already registered with different fields: "
                        f"{project.render_project_id}"
                    )
                return stored
            row = RenderProjectRecord(
                render_project_id=project.render_project_id,
                org_id=project.org_id,
                brief_id=project.brief_id,
                name=project.name,
                spec=dict(project.spec),
                created_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "render_projects.registered",
                "render_projects",
                f"render_project:{project.render_project_id}",
                {
                    "org_id": project.org_id,
                    "brief_id": project.brief_id,
                    "spec_version": project.spec.get("version"),
                    "output_count": len(project.spec.get("outputs") or []),
                },
            )
            return _project_from_row(row)

    def get_project(self, render_project_id: str, org_id: str) -> RenderProject | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == render_project_id,
                    RenderProjectRecord.org_id == org_id,
                )
            ).first()
            return _project_from_row(row) if row is not None else None

    def list_projects(self, org_id: str, brief_id: str) -> list[RenderProject]:
        with self._sessions() as session:
            brief = session.scalars(
                sa.select(ContentBriefRecord).where(
                    ContentBriefRecord.brief_id == brief_id,
                    ContentBriefRecord.org_id == org_id,
                )
            ).first()
            if brief is None:
                raise ValueError(f"content brief not found: {brief_id}")
            rows = session.scalars(
                sa.select(RenderProjectRecord)
                .where(
                    RenderProjectRecord.org_id == org_id,
                    RenderProjectRecord.brief_id == brief_id,
                )
                .order_by(RenderProjectRecord.created_at, RenderProjectRecord.id)
            ).all()
            return [_project_from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["render-projects"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RenderProjectStore:
    return RenderProjectStore.from_engine(engine)


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
    "/render-specs/validate",
    response_model=ValidationResult,
    name="validate_render_spec_route",
)
def validate_render_spec_route(request: RenderSpecValidateRequest) -> ValidationResult:
    """Dry-run the DSL validator (pure — no persistence, no DB)."""
    errors = validate_render_spec(request.spec)
    return ValidationResult(valid=not errors, errors=errors)


@router.post(
    "/render-projects",
    response_model=RenderProject,
    name="register_render_project",
)
def register_render_project_route(
    request: RenderProject,
    store: Annotated[RenderProjectStore, Depends(_store)],
) -> RenderProject:
    """Register one render project (spec validated, brief-gated, idempotent)."""
    try:
        return store.register_project(request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/render-projects/{render_project_id}",
    response_model=RenderProject,
    name="get_render_project",
)
def get_render_project_route(
    render_project_id: Annotated[str, Path(min_length=1, max_length=128)],
    store: Annotated[RenderProjectStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> RenderProject:
    """Get one render project by id and org."""
    stored = store.get_project(render_project_id, org_id)
    if stored is None:
        raise HTTPException(
            status_code=404, detail=f"render project not found: {render_project_id}"
        )
    return stored


@router.get(
    "/render-projects",
    response_model=list[RenderProject],
    name="list_render_projects",
)
def list_render_projects_route(
    store: Annotated[RenderProjectStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    brief_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[RenderProject]:
    """List render projects for one brief (404 when the brief is unknown)."""
    try:
        return store.list_projects(org_id, brief_id)
    except ValueError as exc:
        raise _map_error(exc) from exc
