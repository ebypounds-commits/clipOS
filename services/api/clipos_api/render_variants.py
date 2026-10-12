"""Platform aspect variants, QC reports, and output manifests (CLIP-053).

Closes M12 with the fanout layer over CLIP-051/052 renders:

- **Aspect variants** — one job renders every ``outputs[]`` entry, each
  with its own aspect (``outputs[i].aspect`` override, defaulting to the
  spec composition aspect) into ``workdir/<job_id>/<platform>.mp4``.
  Platform slugs are filename-sanitized (``[A-Za-z0-9._-]+``); paths are
  workdir-confined (CLIP-039 policy).
- **QC** — after each variant, ffprobe (env ``FFMPEG_BIN`` sibling
  ``FFPROBE_BIN``, default ``ffprobe``, 30s timeout) verifies: file
  exists/size > 0, duration within ±15% of the spec window, resolution
  exactly matching the aspect (EXPECTED_RESOLUTIONS). Pure
  ``evaluate_qc`` decides PASS/FAIL; results persist as ``qc_reports``.
- **Render manifests** — every successful variant stores a deterministic
  per-output manifest on ``render_outputs`` (fingerprint, argv, paths,
  qc status); the job manifest aggregates them.

Lifecycle: variants execute honors the same gates as ``execute_job``
(PENDING/FAILED only; SUCCEEDED/RUNNING -> 409; prep failures -> 422
with the job untouched). A re-run after FAILED clears prior outputs/QC
rows for that job first (fresh attempt, no orphans).

Audits: ``render_outputs.rendered``, ``qc_reports.recorded``, plus the
existing ``render_jobs.succeeded``/``failed``. No LLM, no provider
identifiers (platform slugs only).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi import Path as ApiPath
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from clipos_api import audit
from clipos_api.render_pipeline import (
    DEFAULT_RENDER_TIMEOUT_SECONDS,
    EXPECTED_RESOLUTIONS,
    RenderJob,
    RenderJobRecord,
    RenderJobStatus,
    RenderRunner,
    build_render_argv,
    confine_path,
    prep_job_assets,
    render_workdir,
    spec_fingerprint,
)
from clipos_api.render_project import ALLOWED_ASPECTS, RenderProjectRecord
from clipos_api.repository import Base

#: QC duration tolerance (±15% of the spec window).
QC_DURATION_TOLERANCE = 0.15

#: Platform slugs allowed inside output filenames.
PLATFORM_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class RenderOutput(BaseModel):
    """One rendered platform variant artifact with its manifest."""

    model_config = ConfigDict(frozen=True)

    output_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    job_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=64)
    aspect: str = Field(min_length=3, max_length=8)
    output_path: str = Field(min_length=1, max_length=1024)
    manifest: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class QcReport(BaseModel):
    """One QC verdict for one rendered output."""

    model_config = ConfigDict(frozen=True)

    report_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    output_id: str = Field(min_length=1, max_length=128)
    status: Literal["PASSED", "FAILED"]
    checks: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class RenderOutputRecord(Base):
    """SQL row for one rendered platform variant."""

    __tablename__ = "render_outputs"
    __table_args__ = (
        sa.Index(
            "uq_render_outputs_org_job_platform",
            "org_id",
            "job_id",
            "platform",
            unique=True,
        ),
        sa.Index("ix_render_outputs_org_job", "org_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    output_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    platform: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    aspect: Mapped[str] = mapped_column(sa.String(8), nullable=False)
    output_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


class QcReportRecord(Base):
    """SQL row for one QC report."""

    __tablename__ = "qc_reports"
    __table_args__ = (
        sa.Index("uq_qc_reports_org_output", "org_id", "output_id", unique=True),
        sa.Index("ix_qc_reports_org_job", "org_id", "job_id"),
    )

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    org_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    output_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    job_id: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _output_from_row(row: RenderOutputRecord) -> RenderOutput:
    return RenderOutput(
        output_id=row.output_id,
        org_id=row.org_id,
        job_id=row.job_id,
        platform=row.platform,
        aspect=row.aspect,
        output_path=row.output_path,
        manifest=dict(row.manifest or {}),
        created_at=_aware(row.created_at),
    )


def _qc_from_row(row: QcReportRecord) -> QcReport:
    return QcReport(
        report_id=row.report_id,
        org_id=row.org_id,
        output_id=row.output_id,
        status=row.status,  # type: ignore[arg-type]
        checks=dict(row.checks or {}),
        created_at=_aware(row.created_at),
    )


def probe_output(output_path: str, timeout_seconds: int = 30) -> dict[str, Any]:
    """Run ffprobe (env FFPROBE_BIN, default ffprobe) and parse JSON (pure IO).

    Raises ValueError when ffprobe fails or emits unparsable JSON — callers
    map that to a QC failure, never a crash.
    """
    binary = os.environ.get("FFPROBE_BIN", "ffprobe")
    try:
        completed = subprocess.run(
            [
                binary,
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"ffprobe failed: {exc}") from exc
    if completed.returncode != 0:
        raise ValueError(f"ffprobe failed: {(completed.stderr or '').strip()[:200]}")
    try:
        parsed: dict[str, Any] = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("ffprobe emitted invalid JSON") from exc
    if type(parsed) is not dict:
        raise ValueError("ffprobe emitted non-object JSON")
    return parsed


def evaluate_qc(
    probe: dict[str, Any],
    *,
    aspect: str,
    duration_ms: int,
    size_bytes: int,
) -> tuple[bool, dict[str, Any]]:
    """Pure QC verdict from a probe payload (CLIP-053 check set).

    PASS requires: size > 0, probe duration within ±15% of the spec
    window, and exact width/height for the aspect. Returns
    (passed, checks_dict).
    """
    raw_fmt = probe.get("format")
    fmt = raw_fmt if isinstance(raw_fmt, dict) else {}
    raw_streams = probe.get("streams")
    streams = raw_streams if isinstance(raw_streams, list) else []
    video = next(
        (
            stream
            for stream in streams
            if isinstance(stream, dict) and stream.get("codec_type") == "video"
        ),
        {},
    )

    duration_ok = False
    duration_actual: float | None = None
    raw_duration = fmt.get("duration")
    if raw_duration is not None:
        try:
            duration_actual = float(raw_duration)
            expected = duration_ms / 1000.0
            lower = expected * (1 - QC_DURATION_TOLERANCE)
            upper = expected * (1 + QC_DURATION_TOLERANCE)
            duration_ok = lower <= duration_actual <= upper
        except (TypeError, ValueError):
            duration_ok = False

    expected_width, expected_height = EXPECTED_RESOLUTIONS.get(aspect, (0, 0))
    width = video.get("width")
    height = video.get("height")
    resolution_ok = width == expected_width and height == expected_height

    size_ok = size_bytes > 0
    checks = {
        "size_ok": size_ok,
        "size_bytes": size_bytes,
        "duration_ok": duration_ok,
        "duration_actual": duration_actual,
        "duration_expected_ms": duration_ms,
        "resolution_ok": resolution_ok,
        "width": width,
        "height": height,
        "expected_width": expected_width,
        "expected_height": expected_height,
    }
    passed = size_ok and duration_ok and resolution_ok
    return passed, checks


class RenderVariantStore:
    """Typed persistence + execution for platform variants and QC."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> RenderVariantStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    def execute_variants(
        self,
        runner: RenderRunner,
        job_id: str,
        org_id: str,
        input_path: str,
        *,
        prober: Any = None,
    ) -> RenderJob:
        """Render every spec output as an aspect variant with QC.

        Same gates as RenderJobStore.execute_job: confined input, job 404,
        SUCCEEDED/RUNNING 409, prep failures 422 (job untouched). Each
        output aspect defaults to the spec composition aspect; invalid
        aspect or unsafe platform slug -> 422 before RUNNING. A re-run
        clears the job's previous outputs/QC rows first. Success aggregates
        per-variant manifests + qc statuses onto the job manifest; any
        ffmpeg or QC failure marks the job FAILED (error_ref set) while
        keeping completed rows for diagnosis. ``prober`` injects the
        ffprobe backend (default: real ``probe_output``).
        """
        if prober is None:
            prober = probe_output
        now = datetime.now(UTC)
        confined_input = confine_path(input_path, must_exist=True)

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
            outputs = spec.get("outputs")
            if not isinstance(outputs, list) or not outputs:
                raise ValueError("render project spec has no outputs")

            # Validate every variant plan before touching job status.
            plans: list[tuple[str, str]] = []
            for output in outputs:
                if type(output) is not dict:
                    raise ValueError("render project spec outputs must be objects")
                platform = output.get("platform")
                if type(platform) is not str or not PLATFORM_SLUG_RE.match(platform):
                    raise ValueError(f"unsafe platform slug: {platform!r}")
                aspect = output.get("aspect") or composition.get("aspect")
                if aspect not in ALLOWED_ASPECTS:
                    raise ValueError(f"unsupported aspect for {platform}: {aspect!r}")
                plans.append((platform, aspect))

            srt_path, logo_path = prep_job_assets(
                session,
                job_id=job_id,
                org_id=org_id,
                render_project_id=job.render_project_id,
                spec=spec,
            )

            # Fresh attempt: drop rows from any prior FAILED run.
            session.execute(
                sa.delete(QcReportRecord).where(
                    QcReportRecord.org_id == org_id,
                    QcReportRecord.job_id == job_id,
                )
            )
            session.execute(
                sa.delete(RenderOutputRecord).where(
                    RenderOutputRecord.org_id == org_id,
                    RenderOutputRecord.job_id == job_id,
                )
            )

            job.status = RenderJobStatus.RUNNING.value
            job.error_ref = None
            job.updated_at = now
            session.flush()

            variant_dir = confine_path(str(render_workdir() / job_id))
            variant_dir.mkdir(parents=True, exist_ok=True)
            timeout_seconds = int(
                os.environ.get("RENDER_FFMPEG_TIMEOUT_SECONDS", str(DEFAULT_RENDER_TIMEOUT_SECONDS))
            )
            fingerprint = spec_fingerprint(spec)
            duration_ms = composition.get("duration_ms")
            if not isinstance(duration_ms, int) or duration_ms <= 0:
                job.status = RenderJobStatus.FAILED.value
                job.error_ref = "spec missing valid duration_ms"
                job.updated_at = datetime.now(UTC)
                session.flush()
                audit.record(
                    "render_jobs.failed",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {"org_id": org_id, "error": "spec missing valid duration_ms"},
                )
                return _job_view(job)

            summaries: list[dict[str, Any]] = []
            failure: str | None = None
            for platform, aspect in plans:
                out_path = variant_dir / f"{platform}.mp4"
                try:
                    argv = build_render_argv(
                        str(confined_input),
                        str(out_path),
                        spec,
                        srt_path=srt_path,
                        logo_path=logo_path,
                        aspect_override=aspect,
                    )
                except ValueError as exc:
                    failure = str(exc)
                    break
                result = runner.run(argv, timeout_seconds)
                if result.returncode != 0:
                    failure = result.stderr_tail or f"ffmpeg exit {result.returncode}"
                    break
                manifest = {
                    "spec_fingerprint": fingerprint,
                    "argv": argv,
                    "input_path": str(confined_input),
                    "output_path": str(out_path),
                    "platform": platform,
                    "aspect": aspect,
                    "duration_ms": duration_ms,
                    "captions_burned": srt_path is not None,
                    "logo_overlaid": logo_path is not None,
                    "returncode": 0,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
                output_id = f"{job_id}:{platform}"[:128]
                output_row = RenderOutputRecord(
                    output_id=output_id,
                    org_id=org_id,
                    job_id=job_id,
                    platform=platform,
                    aspect=aspect,
                    output_path=str(out_path.relative_to(render_workdir())),
                    manifest=manifest,
                    created_at=datetime.now(UTC),
                )
                session.add(output_row)
                session.flush()
                audit.record(
                    "render_outputs.rendered",
                    "render_outputs",
                    f"render_output:{output_id}",
                    {
                        "org_id": org_id,
                        "job_id": job_id,
                        "platform": platform,
                        "aspect": aspect,
                    },
                )

                # QC: probe failure counts as a failed report, not a crash.
                try:
                    probe = prober(str(out_path))
                    size_bytes = 0
                    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
                    raw_size = fmt.get("size")
                    if raw_size is not None:
                        try:
                            size_bytes = int(raw_size)
                        except (TypeError, ValueError):
                            size_bytes = 0
                    if size_bytes <= 0 and out_path.is_file():
                        size_bytes = out_path.stat().st_size
                    passed, checks = evaluate_qc(
                        probe, aspect=aspect, duration_ms=duration_ms, size_bytes=size_bytes
                    )
                except ValueError as exc:
                    passed = False
                    checks = {"probe_error": str(exc)[:200]}
                status = "PASSED" if passed else "FAILED"
                report_row = QcReportRecord(
                    report_id=f"{output_id}:qc"[:128],
                    org_id=org_id,
                    output_id=output_id,
                    job_id=job_id,
                    status=status,
                    checks=checks,
                    created_at=datetime.now(UTC),
                )
                session.add(report_row)
                session.flush()
                audit.record(
                    "qc_reports.recorded",
                    "qc_reports",
                    f"qc_report:{report_row.report_id}",
                    {
                        "org_id": org_id,
                        "output_id": output_id,
                        "status": status,
                    },
                )
                summaries.append(
                    {
                        "platform": platform,
                        "aspect": aspect,
                        "output_path": str(out_path.relative_to(render_workdir())),
                        "qc_status": status,
                    }
                )
                if not passed:
                    failure = f"QC failed for platform {platform}"
                    break

            finished = datetime.now(UTC)
            if failure is None:
                job.status = RenderJobStatus.SUCCEEDED.value
                job.manifest = {
                    "spec_fingerprint": fingerprint,
                    "variant_count": len(summaries),
                    "outputs": summaries,
                    "completed_at": finished.isoformat(),
                }
                job.error_ref = None
                job.updated_at = finished
                session.flush()
                audit.record(
                    "render_jobs.succeeded",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {
                        "org_id": org_id,
                        "variant_count": len(summaries),
                        "spec_fingerprint": fingerprint,
                    },
                )
            else:
                job.status = RenderJobStatus.FAILED.value
                job.error_ref = failure[:512]
                job.updated_at = finished
                session.flush()
                audit.record(
                    "render_jobs.failed",
                    "render_jobs",
                    f"render_job:{job_id}",
                    {"org_id": org_id, "error": failure[:128]},
                )
            return _job_view(job)

    def list_outputs(self, org_id: str, job_id: str) -> list[RenderOutput]:
        with self._sessions() as session:
            job = session.scalars(
                sa.select(RenderJobRecord).where(
                    RenderJobRecord.job_id == job_id,
                    RenderJobRecord.org_id == org_id,
                )
            ).first()
            if job is None:
                raise ValueError(f"render job not found: {job_id}")
            rows = session.scalars(
                sa.select(RenderOutputRecord)
                .where(
                    RenderOutputRecord.org_id == org_id,
                    RenderOutputRecord.job_id == job_id,
                )
                .order_by(RenderOutputRecord.created_at, RenderOutputRecord.id)
            ).all()
            return [_output_from_row(row) for row in rows]

    def get_qc_report(self, output_id: str, org_id: str) -> QcReport | None:
        with self._sessions() as session:
            row = session.scalars(
                sa.select(QcReportRecord).where(
                    QcReportRecord.org_id == org_id,
                    QcReportRecord.output_id == output_id,
                )
            ).first()
            return _qc_from_row(row) if row is not None else None


def _job_view(row: RenderJobRecord) -> RenderJob:
    from clipos_api.render_pipeline import _job_from_row

    return _job_from_row(row)


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["render-variants"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> RenderVariantStore:
    return RenderVariantStore.from_engine(engine)


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
    "/render-jobs/{job_id}/variants/execute",
    response_model=RenderJob,
    name="execute_render_variants",
)
def execute_render_variants_route(
    job_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderVariantStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    input_path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> RenderJob:
    """Render every spec output as an aspect variant with QC."""
    from clipos_api.render_pipeline import SubprocessRenderRunner

    try:
        return store.execute_variants(SubprocessRenderRunner(), job_id, org_id, input_path)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/render-jobs/{job_id}/outputs",
    response_model=list[RenderOutput],
    name="list_render_outputs",
)
def list_render_outputs_route(
    job_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderVariantStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> list[RenderOutput]:
    """List rendered variant outputs for one job (404 when unknown)."""
    try:
        return store.list_outputs(org_id, job_id)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/render-outputs/{output_id}/qc-report",
    response_model=QcReport | None,
    name="get_qc_report",
)
def get_qc_report_route(
    output_id: Annotated[str, ApiPath(min_length=1, max_length=128)],
    store: Annotated[RenderVariantStore, Depends(_store)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
) -> QcReport | None:
    """Return the QC report for one output, or null when absent."""
    return store.get_qc_report(output_id, org_id)
