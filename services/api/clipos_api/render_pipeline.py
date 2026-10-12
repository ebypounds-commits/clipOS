"""FFmpeg worker and render pipeline (CLIP-051).

Executes CLIP-050 render specs through a deterministic pipeline:

- `spec_fingerprint` — sha256 over canonical spec JSON (render identity).
- `build_render_argv` — pure argv builder: trim to `duration_ms`, aspect
  variant filter (9:16 / 16:9 / 1:1 pad+scale chains), optional
  `loudnorm` when audio normalization is enabled. Caption burn-in is
  requested in the manifest and executed by CLIP-052 — the manifest is
  the deterministic render record required by 07_SECURITY.
- `RenderRunner` protocol with a real subprocess implementation
  (`SubprocessRenderRunner`, timeout-bounded, `shell=False` argv — no
  injection surface) and test doubles.
- `render_jobs` lifecycle: PENDING -> RUNNING -> SUCCEEDED / FAILED.
  Execution is **workdir-confined**: input and output paths must resolve
  under `RENDER_WORKDIR` (default `/tmp/clipos-render`, CLIP-039 path
  policy), outputs must be `.mp4`, succeeded jobs refuse re-execution
  (409), failed jobs may retry.

Audits: `render_jobs.created`, `render_jobs.succeeded`,
`render_jobs.failed`. Platform entries remain generic slugs; ffmpeg binary
location is env-driven (`FFMPEG_BIN`, default `ffmpeg`) — never a
provider/model identifier.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, Protocol

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.caption_tracks import CaptionSegment, CaptionTrackRecord, segments_to_srt
from clipos_api.render_project import ALLOWED_ASPECTS, RenderProjectRecord
from clipos_api.repository import Base

#: Default filesystem root every render path must stay inside.
DEFAULT_RENDER_WORKDIR = "/tmp/clipos-render"

#: Default per-render ffmpeg timeout (seconds); env-overridable.
DEFAULT_RENDER_TIMEOUT_SECONDS = 120

#: Aspect -> deterministic scale/pad filter chain (H.264-friendly sizes).
ASPECT_FILTERS: dict[str, str] = {
    "9:16": (
        "scale=1080:1920:force_original_aspect_ratio=decrease,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
    ),
    "16:9": (
        "scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"
    ),
    "1:1": (
        "scale=1080:1080:force_original_aspect_ratio=decrease,"
        "pad=1080:1080:(ow-iw)/2:(oh-ih)/2:color=black"
    ),
}

#: Aspect -> exact rendered resolution (QC expectations for CLIP-053).
EXPECTED_RESOLUTIONS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}


class RenderJobStatus(str, Enum):
    """Lifecycle status of one render job."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def spec_fingerprint(spec: dict[str, Any]) -> str:
    """sha256 over canonical (sorted-key, compact) spec JSON (pure)."""
    canonical = json.dumps(spec, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def escape_filter_path(path: str) -> str:
    """Escape a filesystem path for use inside an ffmpeg filter argument (pure)."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def build_render_argv(
    input_path: str,
    output_path: str,
    spec: dict[str, Any],
    *,
    srt_path: str | None = None,
    logo_path: str | None = None,
    aspect_override: str | None = None,
) -> list[str]:
    """Pure deterministic ffmpeg argv for one render spec (no shell).

    Trim + aspect filter always; optional SRT burn-in merged into the
    video filter chain; optional logo overlay via filter_complex
    (top-right, 16px inset) which switches from -vf to -filter_complex
    with explicit -map; loudnorm appended when audio normalization is
    enabled. Caption segment data arrives via ``srt_path`` (CLIP-052
    tracks); logo is the workdir-confined ``inputs.logo_ref`` file.
    """
    composition = spec.get("composition", {})
    aspect = aspect_override if aspect_override is not None else composition.get("aspect")
    if aspect not in ALLOWED_ASPECTS:
        raise ValueError(f"unsupported aspect: {aspect}")
    duration_ms = composition.get("duration_ms")
    if not isinstance(duration_ms, int) or duration_ms <= 0:
        raise ValueError("composition.duration_ms must be a positive int")
    duration_seconds = f"{duration_ms / 1000:.3f}"

    argv: list[str] = [
        os.environ.get("FFMPEG_BIN", "ffmpeg"),
        "-y",
        "-ss",
        "0",
        "-i",
        str(input_path),
    ]
    if logo_path is not None:
        argv.extend(["-i", str(logo_path)])
    argv.extend(["-t", duration_seconds])

    video_chain = [ASPECT_FILTERS[aspect]]
    if srt_path is not None:
        video_chain.append(f"subtitles=filename='{escape_filter_path(srt_path)}'")
    joined_chain = ",".join(video_chain)

    if logo_path is not None:
        filter_complex = f"[0:v]{joined_chain},format=yuv420p[base];[base][1:v]overlay=W-w-16:16[v]"
        argv.extend(["-filter_complex", filter_complex, "-map", "[v]", "-map", "0:a?"])
    else:
        argv.extend(["-vf", joined_chain])

    audio = composition.get("audio_normalization")
    if isinstance(audio, dict) and audio.get("enabled") is True:
        target = audio.get("target_lufs")
        if not isinstance(target, (int, float)) or isinstance(target, bool):
            raise ValueError("audio target_lufs required when normalization enabled")
        argv.extend(["-af", f"loudnorm=I={float(target)}"])

    argv.extend(["-movflags", "+faststart", str(output_path)])
    return argv


def prep_job_assets(
    session: Session,
    *,
    job_id: str,
    org_id: str,
    render_project_id: str,
    spec: dict[str, Any],
) -> tuple[str | None, str | None]:
    """CLIP-052 asset prep shared by all execute paths.

    Renders the latest caption track (when enabled) to a workdir-confined
    SRT and resolves inputs.logo_ref inside the workdir. Must run BEFORE
    the RUNNING stamp: every failure raises ValueError -> route 422 with
    the job left untouched (retryable). Returns (srt_path, logo_path).
    """
    composition = spec.get("composition", {})
    srt_path: str | None = None
    captions = composition.get("captions")
    if isinstance(captions, dict) and captions.get("enabled") is True:
        track = session.scalars(
            sa.select(CaptionTrackRecord)
            .where(
                CaptionTrackRecord.org_id == org_id,
                CaptionTrackRecord.render_project_id == render_project_id,
            )
            .order_by(CaptionTrackRecord.created_at.desc(), CaptionTrackRecord.id.desc())
        ).first()
        if track is not None:
            # confine: job ids are slug-patterned at request time, and the
            # resolve() guard also blocks legacy traversal-shaped ids.
            srt_file = confine_path(str(render_workdir() / f"{job_id}.srt"))
            try:
                srt_file.write_text(
                    segments_to_srt(
                        [CaptionSegment(**segment) for segment in (track.segments or [])]
                    ),
                    encoding="utf-8",
                )
            except OSError as exc:
                raise ValueError(f"caption write failed: {exc}") from exc
            srt_path = str(srt_file)

    logo_path: str | None = None
    inputs = spec.get("inputs")
    logo_ref = inputs.get("logo_ref") if isinstance(inputs, dict) else None
    if logo_ref is not None:
        if not isinstance(logo_ref, str) or not (1 <= len(logo_ref) <= 256):
            raise ValueError("inputs.logo_ref must be a string of 1..256 chars")
        logo_path = str(confine_path(str(render_workdir() / logo_ref), must_exist=True))
    return srt_path, logo_path


@dataclass(frozen=True)
class RenderResult:
    """Outcome of one runner invocation."""

    returncode: int
    stderr_tail: str


class RenderRunner(Protocol):
    """Execution backend for built ffmpeg argv lists."""

    def run(self, argv: list[str], timeout_seconds: int) -> RenderResult: ...


class SubprocessRenderRunner:
    """Real ffmpeg execution: argv list, no shell, timeout-bounded."""

    def run(self, argv: list[str], timeout_seconds: int) -> RenderResult:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return RenderResult(returncode=124, stderr_tail="ffmpeg timeout")
        except OSError as exc:
            return RenderResult(returncode=127, stderr_tail=str(exc)[:512])
        stderr = completed.stderr or ""
        return RenderResult(returncode=completed.returncode, stderr_tail=stderr[-512:])


class RenderJobRequest(BaseModel):
    """Request body for creating one render job."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class RenderExecuteRequest(BaseModel):
    """Request body for executing one render job (confined paths)."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    input_path: str = Field(min_length=1, max_length=1024)
    output_path: str = Field(min_length=1, max_length=1024)


class RenderJob(BaseModel):
    """One render job; natural key = (org_id, job_id)."""

    model_config = ConfigDict(frozen=True)

    job_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    render_project_id: str = Field(min_length=1, max_length=128)
    status: RenderJobStatus
    spec_fingerprint: str = Field(min_length=64, max_length=64)
    manifest: dict[str, Any] | None = None
    error_ref: str | None = Field(default=None, max_length=512)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RenderJobRecord(Base):
    """SQL row for one render job."""

    __tablename__ = "render_jobs"
    __table_args__ = (
        sa.Index("uq_render_jobs_org_job", "org_id", "job_id", unique=True),
        sa.Index("ix_render_jobs_org_project", "org_id", "render_project_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    render_project_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    spec_fingerprint: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    manifest: Mapped[dict[str, Any] | None] = mapped_column(sa.JSON, nullable=True)
    error_ref: Mapped[str | None] = mapped_column(sa.String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _job_from_row(row: RenderJobRecord) -> RenderJob:
    return RenderJob(
        job_id=row.job_id,
        org_id=row.org_id,
        render_project_id=row.render_project_id,
        status=RenderJobStatus(row.status),
        spec_fingerprint=row.spec_fingerprint,
        manifest=dict(row.manifest) if row.manifest is not None else None,
        error_ref=row.error_ref,
        created_at=_aware(row.created_at),
        updated_at=_aware(row.updated_at),
    )


def render_workdir() -> Path:
    """Resolved filesystem root every render path must stay inside."""
    return Path(os.environ.get("RENDER_WORKDIR", DEFAULT_RENDER_WORKDIR)).resolve()


def confine_path(raw_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a render path and enforce the workdir confinement policy.

    Raises ValueError (route maps to 422) when the resolved path escapes
    RENDER_WORKDIR or, when required, does not exist as a regular file.
    """
    resolved = Path(raw_path).resolve()
    root = render_workdir()
    if not resolved.is_relative_to(root):
        raise ValueError(f"path outside render workdir: {raw_path}")
    if must_exist and not resolved.is_file():
        raise ValueError(f"input file missing or unreadable: {raw_path}")
    return resolved


class RenderJobStore:
    """Typed persistence for render jobs plus the execution flow."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RenderJobStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def get_job(self, job_id: str, org_id: str) -> RenderJob | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(RenderJobRecord).where(
                    RenderJobRecord.job_id == job_id,
                    RenderJobRecord.org_id == org_id,
                )
            ).first()
            return _job_from_row(row) if row is not None else None

    def list_jobs(self, org_id: str, render_project_id: str) -> list[RenderJob]:
        with self._sessions() as session:
            project = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == render_project_id,
                    RenderProjectRecord.org_id == org_id,
                )
            ).first()
            if project is None:
                raise ValueError(f"render project not found: {render_project_id}")
            rows = session.scalars(
                sa.select(RenderJobRecord)
                .where(
                    RenderJobRecord.org_id == org_id,
                    RenderJobRecord.render_project_id == render_project_id,
                )
                .order_by(RenderJobRecord.created_at, RenderJobRecord.id)
            ).all()
            return [_job_from_row(row) for row in rows]

    def create_job_for_project(
        self, request: RenderJobRequest, render_project_id: str
    ) -> RenderJob:
        """Create one PENDING job bound to a render project (idempotent)."""
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            project = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == render_project_id,
                    RenderProjectRecord.org_id == request.org_id,
                )
            ).first()
            if project is None:
                raise ValueError(f"render project not found: {render_project_id}")
            existing = session.scalars(
                sa.select(RenderJobRecord).where(
                    RenderJobRecord.org_id == request.org_id,
                    RenderJobRecord.job_id == request.job_id,
                )
            ).first()
            if existing is not None:
                stored = _job_from_row(existing)
                if stored.render_project_id != render_project_id:
                    raise ValueError(
                        f"render job already registered with different fields: {request.job_id}"
                    )
                return stored
            row = RenderJobRecord(
                job_id=request.job_id,
                org_id=request.org_id,
                render_project_id=render_project_id,
                status=RenderJobStatus.PENDING.value,
                spec_fingerprint=spec_fingerprint(dict(project.spec or {})),
                manifest=None,
                error_ref=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            audit.record(
                "render_jobs.created",
                "render_jobs",
                f"render_job:{request.job_id}",
                {
                    "org_id": request.org_id,
                    "render_project_id": render_project_id,
                    "spec_fingerprint": row.spec_fingerprint,
                },
            )
            return _job_from_row(row)

    def execute_job(
        self,
        runner: RenderRunner,
        job_id: str,
        org_id: str,
        input_path: str,
        output_path: str,
    ) -> RenderJob:
        """Run one render job with workdir-confined paths.

        Job must exist (404); SUCCEEDED/RUNNING refuse re-execution (409);
        paths must resolve inside RENDER_WORKDIR (422), the input must
        exist, the output must end in .mp4. When the spec enables
        captions, the latest caption track for the project is rendered to
        workdir/<job_id>.srt and burned in (absent track -> clean render
        with captions_burned=false); inputs.logo_ref (workdir-relative)
        is overlaid when present and fails the request (422) when missing
        — both preps run before the RUNNING stamp so failures stay
        retryable. PENDING and FAILED are executable. Success stores the
        deterministic manifest; failure stores the stderr tail.
        """
        now = datetime.now(UTC)
        confined_input = confine_path(input_path, must_exist=True)
        confined_output = confine_path(output_path)
        if confined_output.suffix.lower() != ".mp4":
            raise ValueError("output_path must end with .mp4")

        with self._sessions.begin() as session:
            job = session.scalars(
                sa.select(RenderJobRecord).where(
                    RenderJobRecord.job_id == job_id,
                    RenderJobRecord.org_id == org_id,
                )
            ).first()
            if job is None:
                raise ValueError(f"render job not found: {job_id}")
            if job.status in (
                RenderJobStatus.SUCCEEDED.value,
                RenderJobStatus.RUNNING.value,
            ):
                raise ValueError(f"render job already in status {job.status}: {job_id}")
            project = session.scalars(
                sa.select(RenderProjectRecord).where(
                    RenderProjectRecord.render_project_id == job.render_project_id,
                    RenderProjectRecord.org_id == org_id,
                )
            ).first()
            if project is None:
                raise ValueError(f"render project not found: {job.render_project_id}")

            spec = dict(project.spec or {})
            composition = spec.get("composition", {})
            captions = composition.get("captions")

            # --- CLIP-052 prep (shared): failures -> 422, job stays PENDING.
            srt_path, logo_path = prep_job_assets(
                session,
                job_id=job_id,
                org_id=org_id,
                render_project_id=job.render_project_id,
                spec=spec,
            )

            job.status = RenderJobStatus.RUNNING.value
            job.updated_at = now
            session.flush()

            try:
                argv = build_render_argv(
                    str(confined_input),
                    str(confined_output),
                    spec,
                    srt_path=srt_path,
                    logo_path=logo_path,
                )
            except ValueError as exc:
                job.status = RenderJobStatus.FAILED.value
                job.error_ref = str(exc)[:512]
                job.updated_at = datetime.now(UTC)
                session.flush()
                audit.record(
                    "render_jobs.failed",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {"org_id": org_id, "error": str(exc)[:128]},
                )
                return _job_from_row(job)

            timeout_seconds = int(
                os.environ.get("RENDER_FFMPEG_TIMEOUT_SECONDS", str(DEFAULT_RENDER_TIMEOUT_SECONDS))
            )
            result = runner.run(argv, timeout_seconds)
            finished = datetime.now(UTC)
            audio = composition.get("audio_normalization")
            if result.returncode == 0:
                manifest = {
                    "spec_fingerprint": spec_fingerprint(spec),
                    "argv": argv,
                    "input_path": str(confined_input),
                    "output_path": str(confined_output),
                    "duration_ms": composition.get("duration_ms"),
                    "aspect": composition.get("aspect"),
                    "captions_requested": bool(
                        isinstance(captions, dict) and captions.get("enabled")
                    ),
                    "captions_burned": srt_path is not None,
                    "logo_overlaid": logo_path is not None,
                    "audio_normalized": bool(isinstance(audio, dict) and audio.get("enabled")),
                    "returncode": 0,
                    "completed_at": finished.isoformat(),
                }
                job.status = RenderJobStatus.SUCCEEDED.value
                job.manifest = manifest
                job.error_ref = None
                job.updated_at = finished
                session.flush()
                audit.record(
                    "render_jobs.succeeded",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {
                        "org_id": org_id,
                        "spec_fingerprint": manifest["spec_fingerprint"],
                        "aspect": manifest["aspect"],
                    },
                )
            else:
                job.status = RenderJobStatus.FAILED.value
                job.error_ref = result.stderr_tail or f"ffmpeg exit {result.returncode}"
                job.updated_at = finished
                session.flush()
                audit.record(
                    "render_jobs.failed",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {
                        "org_id": org_id,
                        "returncode": result.returncode,
                        "error_prefix": (result.stderr_tail or "")[:128],
                    },
                )
            return _job_from_row(job)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["render-jobs"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RenderJobStore:
    return RenderJobStore.from_engine(engine)


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
    "/render-projects/{render_project_id}/jobs",
    response_model=RenderJob,
    name="create_render_job",
)
def create_render_job_route(
    request: RenderJobRequest,
    render_project_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderJobStore, Depends(_store)],
) -> RenderJob:
    """Create one PENDING render job for a project (idempotent)."""
    try:
        return store.create_job_for_project(request, render_project_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.post(
    "/render-jobs/{job_id}/execute",
    response_model=RenderJob,
    name="execute_render_job",
)
def execute_render_job_route(
    request: RenderExecuteRequest,
    job_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderJobStore, Depends(_store)],
) -> RenderJob:
    """Execute one render job (workdir-confined paths, real ffmpeg)."""
    try:
        return store.execute_job(
            SubprocessRenderRunner(),
            job_id,
            request.org_id,
            request.input_path,
            request.output_path,
        )
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/render-jobs/{job_id}",
    response_model=RenderJob,
    name="get_render_job",
)
def get_render_job_route(
    job_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderJobStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> RenderJob:
    """Get one render job by id and org."""
    stored = store.get_job(job_id, org_id)
    if stored is None:
        raise HTTPException(status_code=404, detail=f"render job not found: {job_id}")
    return stored


@router.get(
    "/render-jobs",
    response_model=list[RenderJob],
    name="list_render_jobs",
)
def list_render_jobs_route(
    store: Annotated[RenderJobStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    render_project_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[RenderJob]:
    """List jobs for one render project (404 when the project is unknown)."""
    try:
        return store.list_jobs(org_id, render_project_id)
    except ValueError as exc:
        raise _map_error(exc) from exc
