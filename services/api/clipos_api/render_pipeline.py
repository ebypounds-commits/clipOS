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


def build_render_argv(input_path: str, output_path: str, spec: dict[str, Any]) -> list[str]:
    """Pure deterministic ffmpeg argv for one render spec (no shell).

    Trim + aspect filter always; loudnorm appended when audio
    normalization is enabled; faststart flags for mp4 output. Caption
    burn-in is deliberately absent (CLIP-052) — callers read
    ``composition.captions.enabled`` for the manifest flag.
    """
    composition = spec.get("composition", {})
    aspect = composition.get("aspect")
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
        "-t",
        duration_seconds,
        "-vf",
        ASPECT_FILTERS[aspect],
    ]

    audio = composition.get("audio_normalization")
    if isinstance(audio, dict) and audio.get("enabled") is True:
        target = audio.get("target_lufs")
        if not isinstance(target, (int, float)) or isinstance(target, bool):
            raise ValueError("audio target_lufs required when normalization enabled")
        argv.extend(["-af", f"loudnorm=I={float(target)}"])

    argv.extend(["-movflags", "+faststart", str(output_path)])
    return argv


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
    job_id: str = Field(min_length=1, max_length=128)


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
        exist, the output must end in .mp4. PENDING and FAILED are
        executable (retry-friendly). Success stores the deterministic
        manifest; failure stores the stderr tail as error_ref.
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

            job.status = RenderJobStatus.RUNNING.value
            job.updated_at = now
            session.flush()

            spec = dict(project.spec or {})
            try:
                argv = build_render_argv(str(confined_input), str(confined_output), spec)
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
            composition = spec.get("composition", {})
            captions = composition.get("captions")
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
