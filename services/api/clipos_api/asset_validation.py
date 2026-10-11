"""Asset validation and quarantine for media files using ffprobe (CLIP-039).

Implements:
- FFprobeValidator: calls /usr/bin/ffprobe with 30s timeout to determine media validity
- validate_and_record: runs validator, writes asset_validations, upserts quarantine record
- Asset validation and quarantine state machine per media_asset_id (idempotent)
- Audit transitions: media_ingestion.validation_recorded, quarantined, released
- Integration with media_assets status: REJECTED on QUARANTINED, READY on VALID
- Router for FastAPI endpoints
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Column, DateTime, String, Table, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from clipos_api import audit as audit_mod
from clipos_api.media_ingestion import MediaAssetRecord, MediaAssetStatus
from clipos_api.repository import Base

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pydantic models
# -------------------------------------------------------------------------__PYDANTIC


class ValidationResult(BaseModel):
    """Result from ffprobe validation."""

    model_config = ConfigDict(frozen=True)

    status: str = Field(
        min_length=1,
        max_length=16,
        description="One of: VALID, QUARANTINED",
    )
    reason: str | None = Field(
        default=None,
        max_length=512,
        description="Error reason when QUARANTINED; None when VALID",
    )
    probe_summary: str = Field(
        default="",
        description="JSON probe output ref or summary (max 512 chars)",
        max_length=512,
    )


# ---------------------------------------------------------------------------
# SQL tables (append-only, frozen Base-aligned)
# -------------------------------------------------------------------------__SQL


asset_validations_table = Table(
    "asset_validations",
    Base.metadata,
    Column("validation_id", String(36), primary_key=True),
    Column("media_asset_id", String(128), nullable=False),
    Column("org_id", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("probe_ref", String(512), nullable=True),
    Column("reason", String(512), nullable=True),
    Column("validated_at", DateTime(timezone=True), nullable=False),
)

quarantine_records_table = Table(
    "quarantine_records",
    Base.metadata,
    Column("quarantine_id", String(36), primary_key=True),
    Column("media_asset_id", String(128), nullable=False),
    Column("org_id", String(64), nullable=False),
    Column("reason", String(512), nullable=False),
    Column("quarantined_at", DateTime(timezone=True), nullable=False),
    Column("released_at", DateTime(timezone=True), nullable=True),
)


# ---------------------------------------------------------------------------
# FFprobeValidator
# -------------------------------------------------------------------------__VALIDATOR


class FFprobeValidator:
    """Run ffprobe against a storage reference and return a ValidationResult."""

    def __init__(self, ffprobe_bin: str = "/usr/bin/ffprobe", timeout: float = 30.0) -> None:
        self.ffprobe_bin = ffprobe_bin
        self.timeout = timeout

    def validate(self, storage_ref: str) -> ValidationResult:
        """Run ffprobe and return ValidationResult.

        - Corrupt/empty files, non-media files, missing paths -> QUARANTINED
          with the ffprobe error as reason.
        - Valid media (streams parsed, duration present) -> VALID.
        """
        if not storage_ref:
            return ValidationResult(
                status="QUARANTINED",
                reason="empty storage reference",
                probe_summary="",
            )

        try:
            result = subprocess.run(
                [
                    self.ffprobe_bin,
                    "-v",
                    "error",
                    "-print_format",
                    "json",
                    "-show_streams",
                    "-show_format",
                    storage_ref,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("ffprobe timeout for %s", storage_ref)
            return ValidationResult(
                status="QUARANTINED",
                reason=f"ffprobe timeout after {self.timeout}s",
                probe_summary="",
            )
        except FileNotFoundError:
            logger.warning("ffprobe binary not found: %s", self.ffprobe_bin)
            return ValidationResult(
                status="QUARANTINED",
                reason="ffprobe binary not found",
                probe_summary="",
            )

        # Parse JSON output
        probe_json: dict | None = None
        if result.stdout.strip():
            try:
                probe_json = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                logger.warning("ffprobe JSON parse error for %s: %s", storage_ref, exc)
                return ValidationResult(
                    status="QUARANTINED",
                    reason=f"ffprobe JSON parse error: {exc}",
                    probe_summary="",
                )

        # Determine validity
        if probe_json is None:
            # No output at all
            return ValidationResult(
                status="QUARANTINED",
                reason=result.stderr.strip() or "ffprobe produced no output",
                probe_summary="",
            )

        # Check for valid media: look for streams and duration in format
        has_streams = probe_json.get("streams") and len(probe_json.get("streams", [])) > 0
        has_duration = probe_json.get("format", {}) and probe_json["format"].get("duration")

        if has_streams and has_duration:
            # Valid media
            probe_summary = json.dumps(probe_json, sort_keys=True)[:512]
            return ValidationResult(
                status="VALID",
                reason="",
                probe_summary=probe_summary,
            )
        else:
            # Not valid media
            error_msg = result.stderr.strip() or "ffprobe: no streams or duration found"
            probe_summary = json.dumps(probe_json, sort_keys=True)[:512] if probe_json else ""
            return ValidationResult(
                status="QUARANTINED",
                reason=error_msg,
                probe_summary=probe_summary,
            )


# ---------------------------------------------------------------------------
# Helper: validate_and_record (exported)
# -------------------------------------------------------------------------__FLOW


def validate_and_record(
    validator: FFprobeValidator,
    store: AssetValidationStore,
    engine: Engine,
    media_asset_id: str,
    org_id: str,
    storage_ref: str,
) -> ValidationResult:
    """Run the validator, write an asset_validations row, and manage quarantine.

    On QUARANTINED: upserts quarantine record + marks media_assets.status REJECTED.
    On VALID: marks media_assets.status READY (idempotent).
    """
    # Run the validator
    result = validator.validate(storage_ref)

    # Record the validation (append-only)
    probe_ref = result.probe_summary or ""
    reason = result.reason or ""
    store.record_validation(
        media_asset_id=media_asset_id,
        org_id=org_id,
        status=result.status,
        probe_ref=probe_ref,
        reason=reason,
    )

    # Manage quarantine state based on validation result
    if result.status == "QUARANTINED":
        # Upsert quarantine record; this will trigger REJECTED status on media_assets
        quarantine_state = store.upsert_quarantine(media_asset_id, org_id, reason)

        # Mark media_assets.status REJECTED
        with sessionmaker(bind=engine, expire_on_commit=False).begin() as session:
            asset_row = session.scalar(
                select(MediaAssetRecord).where(
                    MediaAssetRecord.media_asset_id == media_asset_id,
                    MediaAssetRecord.org_id == org_id,
                )
            )
            if asset_row is not None:
                asset_row.status = MediaAssetStatus.REJECTED.value
                session.flush()
                audit_mod.record(
                    "media_ingestion.quarantined",
                    "media_ingestion",
                    f"media_asset:{media_asset_id}",
                    {
                        "org_id": org_id,
                        "media_asset_id": media_asset_id,
                        "quarantine_id": quarantine_state["quarantine_id"],
                    },
                )

    elif result.status == "VALID":
        # Mark media_assets.status READY (idempotent)
        with sessionmaker(bind=engine, expire_on_commit=False).begin() as session:
            asset_row = session.scalar(
                select(MediaAssetRecord).where(
                    MediaAssetRecord.media_asset_id == media_asset_id,
                    MediaAssetRecord.org_id == org_id,
                )
            )
            if asset_row is not None and asset_row.status != MediaAssetStatus.READY.value:
                asset_row.status = MediaAssetStatus.READY.value
                session.flush()
                audit_mod.record(
                    "media_ingestion.validation_recorded",
                    "media_ingestion",
                    f"media_asset:{media_asset_id}",
                    {
                        "org_id": org_id,
                        "media_asset_id": media_asset_id,
                        "status": "READY",
                    },
                )

    return result


# ---------------------------------------------------------------------------
# Store operations (append-only, frozen-row pattern)
# -------------------------------------------------------------------------__STORE


class AssetValidationStore:
    """Typed persistence for asset_validations and quarantine_records."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    @classmethod
    def from_engine(cls, engine: Engine) -> AssetValidationStore:
        return cls(sessionmaker(bind=engine, expire_on_commit=False))

    # ------------------------------------------------------------------
    # Asset validations (append-only)
    # ------------------------------------------------------------------

    def record_validation(
        self,
        media_asset_id: str,
        org_id: str,
        status: str,
        probe_ref: str | None,
        reason: str | None,
    ) -> None:
        """Append a new validation row (one per attempt)."""
        import uuid

        validation_id = f"val_{str(uuid.uuid4())[:12]}"
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            session.execute(
                insert(asset_validations_table).values(
                    validation_id=validation_id,
                    media_asset_id=media_asset_id,
                    org_id=org_id,
                    status=status,
                    probe_ref=probe_ref,
                    reason=reason,
                    validated_at=now,
                )
            )
            session.flush()
            audit_mod.record(
                "media_ingestion.validation_recorded",
                "media_ingestion",
                f"validation:{validation_id}",
                {
                    "org_id": org_id,
                    "media_asset_id": media_asset_id,
                    "status": status,
                },
            )

    # ------------------------------------------------------------------
    # Quarantine records (idempotent per media_asset_id)
    # ------------------------------------------------------------------

    def upsert_quarantine(
        self,
        media_asset_id: str,
        org_id: str,
        reason: str,
    ) -> dict[str, object]:
        """Upsert quarantine record; idempotent if same reason.

        Returns dict with quarantine state: {quarantine_id, action, released_at}.
        """
        import uuid

        quarantine_id = f"qid_{str(uuid.uuid4())[:12]}"
        now = datetime.now(UTC)

        with self._sessions.begin() as session:
            # Check for existing active quarantine (no released_at)
            existing = session.execute(
                select(quarantine_records_table).where(
                    quarantine_records_table.c.media_asset_id == media_asset_id,
                    quarantine_records_table.c.org_id == org_id,
                    quarantine_records_table.c.released_at.is_(None),
                )
            ).first()

            action: str | None = None

            if existing is None:
                # No active quarantine -> create new record
                session.execute(
                    insert(quarantine_records_table).values(
                        quarantine_id=quarantine_id,
                        media_asset_id=media_asset_id,
                        org_id=org_id,
                        reason=reason,
                        quarantined_at=now,
                        released_at=None,
                    )
                )
                action = "quarantined"
                logger.info(
                    "Quarantining new asset %s/org %s: %s",
                    media_asset_id,
                    org_id,
                    reason[:80],
                )
            elif existing.reason == reason:
                # Same reason -> no-op
                action = "no-op"
                logger.debug(
                    "Re-quarantine with identical reason is no-op for %s",
                    media_asset_id,
                )
            else:
                # Different reason -> update the reason (idempotent per media_asset_id)
                session.execute(
                    quarantine_records_table.update()
                    .where(quarantine_records_table.c.quarantine_id == existing.quarantine_id)
                    .values(reason=reason, quarantined_at=now)
                )
                action = "re-quarantined"
                logger.warning(
                    "Re-quarantining asset %s with different reason (replacing)",
                    media_asset_id,
                )

            session.flush()
            audit_mod.record(
                "media_ingestion.quarantined"
                if action != "no-op"
                else "media_ingestion.quarantine_noop",
                "media_ingestion",
                f"quarantine:{existing.quarantine_id if existing else quarantine_id}",
                {
                    "org_id": org_id,
                    "media_asset_id": media_asset_id,
                    "reason": reason[:80],
                    "action": action,
                },
            )

            return {
                "quarantine_id": existing.quarantine_id if existing else quarantine_id,
                "action": action or "unknown",
                "released_at": existing.released_at if existing else None,
            }

    def release_quarantine(
        self,
        media_asset_id: str,
        org_id: str,
    ) -> dict[str, object]:
        """Release an active quarantine record.

        Sets released_at. After release, the asset may be quarantined again
        only as a NEW record.
        """

        now = datetime.now(UTC)

        with self._sessions.begin() as session:
            # Find active quarantine
            existing_row = session.execute(
                select(quarantine_records_table).where(
                    quarantine_records_table.c.media_asset_id == media_asset_id,
                    quarantine_records_table.c.org_id == org_id,
                    quarantine_records_table.c.released_at.is_(None),
                )
            ).first()

            existing = existing_row
            if existing is None:
                audit_mod.record(
                    "media_ingestion.released",
                    "media_ingestion",
                    "quarantine:no-active",
                    {"org_id": org_id, "media_asset_id": media_asset_id},
                )
                return {
                    "quarantine_id": None,
                    "action": "no-active-quarantine",
                    "released_at": None,
                }

            # Set released_at
            session.execute(
                quarantine_records_table.update()
                .where(quarantine_records_table.c.quarantine_id == existing.quarantine_id)
                .values(released_at=now)
            )
            action = "released"

            audit_mod.record(
                "media_ingestion.released",
                "media_ingestion",
                f"quarantine:{existing.quarantine_id}",
                {
                    "org_id": org_id,
                    "media_asset_id": media_asset_id,
                    "quarantine_id": existing.quarantine_id,
                },
            )

            session.flush()

            return {
                "quarantine_id": existing.quarantine_id,
                "action": action,
                "released_at": now,
            }


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# -------------------------------------------------------------------------__ROUTER


router = APIRouter(tags=["asset-validation"])


def _database_engine() -> Engine:
    """Zero-arg Depends provider over main.database_engine (CLIP-033 rule)."""
    from clipos_api.main import database_engine

    return database_engine()


def _validator() -> FFprobeValidator:
    return FFprobeValidator()


def _store(
    engine: Annotated[Engine, Depends(_database_engine)],
) -> AssetValidationStore:
    return AssetValidationStore.from_engine(engine)


@router.post(
    "/asset-validation/validate-and-record",
    response_model=ValidationResult,
    name="validate_and_record",
)
def validate_and_record_route(
    storage_ref: Annotated[str, Query(min_length=1, max_length=512)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    media_asset_id: Annotated[str, Query(min_length=1, max_length=128)],
    validator: Annotated[FFprobeValidator, Depends(_validator)],
    store: Annotated[AssetValidationStore, Depends(_store)],
    engine: Annotated[Engine, Depends(_database_engine)],
) -> ValidationResult:
    """Validate a media asset with ffprobe and record the result.

    On QUARANTINED: upserts quarantine record and marks media_assets.status REJECTED.
    On VALID: marks media_assets.status READY (idempotent).
    """
    try:
        result = validate_and_record(validator, store, engine, media_asset_id, org_id, storage_ref)
        return result
    except Exception as exc:
        logger.error(
            "validate_and_record failed for %s/org %s: %s",
            media_asset_id,
            org_id,
            exc,
        )
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get(
    "/asset-validation/quarantine/{media_asset_id}",
    response_model=dict,
    name="get_quarantine_state",
)
def get_quarantine_state_route(
    media_asset_id: Annotated[str, Path(min_length=1, max_length=128)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    store: Annotated[AssetValidationStore, Depends(_store)],
) -> dict:
    """Get the current quarantine state for a media asset."""
    with store._sessions() as session:
        row = session.execute(
            select(quarantine_records_table).where(
                quarantine_records_table.c.media_asset_id == media_asset_id,
                quarantine_records_table.c.org_id == org_id,
                quarantine_records_table.c.released_at.is_(None),
            )
        ).first()

        if row is None:
            return {
                "quarantine_active": False,
                "quarantine_id": None,
                "released_at": None,
                "reason": None,
            }

        return {
            "quarantine_active": True,
            "quarantine_id": row.quarantine_id,
            "released_at": row.released_at,
            "reason": row.reason,
            "quarantined_at": row.quarantined_at,
        }


@router.post(
    "/asset-validation/release-quarantine",
    response_model=dict,
    name="release_quarantine",
)
def release_quarantine_route(
    media_asset_id: Annotated[str, Query(min_length=1, max_length=128)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    store: Annotated[AssetValidationStore, Depends(_store)],
    engine: Annotated[Engine, Depends(_database_engine)],
) -> dict:
    """Release an active quarantine record.

    After release, the asset may be quarantined again only as a NEW record.
    """
    try:
        result = store.release_quarantine(media_asset_id, org_id)
        return result
    except Exception as exc:
        logger.error("release_quarantine failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
