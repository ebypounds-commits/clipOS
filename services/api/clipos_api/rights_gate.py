"""Deterministic rights publication gate (CLIP-033).

No-LLM, no-judgment gate: a publication may proceed only when the item's
rights status is CLEAR (or license-backed), composing rights_ledger
effective_status + provenance manifest presence + attribution requirement
satisfaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

from clipos_api import audit
from clipos_api.provenance import ProvenanceStore
from clipos_api.rights_ledger import (
    AttributionRequirementRecord,
    RightsLedgerStore,
    RightsRecord,
    RightsRecordRecord,
    Status,
)


class RightsGateReason(str, Enum):
    """Deterministic gate decision reasons."""

    ALLOWED = "allowed"
    CLEAR = "clear"
    LICENSED_ACTIVE = "licensed_active"
    NOT_FOUND = "not_found"
    STATUS_NOT_CLEAR = "status_not_clear"
    EXPIRED = "expired"
    MISSING_ATTRIBUTION = "missing_attribution"
    MISSING_PROVENANCE = "missing_provenance"


class RightsGateDecision(BaseModel):
    """Decision output of the rights publication gate."""

    allowed: bool
    reason: RightsGateReason
    record_status: Status
    checked_at: datetime


def _requirement_rows(session: Any, record_id: str) -> list[AttributionRequirementRecord]:
    return list(
        session.scalars(
            sa.select(AttributionRequirementRecord).where(
                AttributionRequirementRecord.record_id == record_id
            )
        )
    )


class RightsPublicationGate:
    """Deterministic rights publication gate.

    Composes RightsLedgerStore.effective_status + provenance manifest
    presence + attribution requirement satisfaction. All logic is a pure
    function of the inputs — no LLM, no judgment.
    """

    def __init__(
        self,
        ledger_store: RightsLedgerStore,
        provenance_store: ProvenanceStore | None = None,
    ) -> None:
        self._ledger = ledger_store
        self._provenance = provenance_store

    # ------------------------------------------------------------------
    # Core evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        item_id: str,
        org_id: str,
        now: datetime | None = None,
        *,
        checked_at: datetime | None = None,
    ) -> RightsGateDecision:
        """Evaluate the publication gate for *item_id* in *org_id*.

        Deterministic: same inputs always produce the same decision.
        """
        ts = checked_at or datetime.now(UTC)

        record = self._find_record(item_id, org_id)
        if record is None:
            return RightsGateDecision(
                allowed=False,
                reason=RightsGateReason.NOT_FOUND,
                record_status=Status.UNKNOWN,
                checked_at=ts,
            )

        eff = self._ledger.effective_status(record.record_id, org_id, now=now)
        reqs = self._list_requirements(record.record_id)
        attribution_ok = self._attribution_satisfied(record, reqs)
        manifest_ok = self._manifest_present(item_id, org_id)

        allowed, reason = self._compose(
            eff=eff, reqs=reqs, attribution_ok=attribution_ok, manifest_ok=manifest_ok
        )
        audit.record(
            "rights_gate.evaluate",
            "rights-gate",
            f"rights_record:{record.record_id}",
            {
                "org_id": org_id,
                "item_id": item_id,
                "effective_status": eff.value,
                "allowed": allowed,
                "reason": reason.value,
            },
        )
        return RightsGateDecision(allowed=allowed, reason=reason, record_status=eff, checked_at=ts)

    # ------------------------------------------------------------------
    # Internal composition logic
    # ------------------------------------------------------------------

    def _compose(
        self,
        eff: Status,
        reqs: list[AttributionRequirementRecord],
        attribution_ok: bool,
        manifest_ok: bool,
    ) -> tuple[bool, RightsGateReason]:
        """Pure decision composition: status -> attribution -> provenance."""
        if not reqs or attribution_ok:
            if not manifest_ok:
                return False, RightsGateReason.MISSING_PROVENANCE
            if eff is Status.CLEAR:
                return True, RightsGateReason.CLEAR
            if eff is Status.EXPIRED:
                return False, RightsGateReason.EXPIRED
            return False, RightsGateReason.STATUS_NOT_CLEAR
        return False, RightsGateReason.MISSING_ATTRIBUTION

    def _find_record(self, item_id: str, org_id: str) -> RightsRecord | None:
        with self._ledger._sessions() as session:
            row = session.scalars(
                sa.select(RightsRecordRecord).where(
                    RightsRecordRecord.item_id == item_id,
                    RightsRecordRecord.org_id == org_id,
                )
            ).first()
        if row is None:
            return None
        # Build directly (bypasses the initial-status validator): records may
        # legitimately be in post-transition states (CLEAR/REVOKED/EXPIRED).
        from clipos_api.rights_ledger import Basis, _aware

        return RightsRecord.model_construct(
            record_id=row.record_id,
            item_id=row.item_id,
            org_id=row.org_id,
            status=Status(row.status),
            basis=Basis(row.basis),
            reviewed_by=row.reviewed_by,
            metadata=row.record_metadata or {},
            created_at=_aware(row.created_at),
        )

    def _list_requirements(self, record_id: str) -> list[AttributionRequirementRecord]:
        with self._ledger._sessions() as session:
            return _requirement_rows(session, record_id)

    def _attribution_satisfied(
        self, record: RightsRecord, reqs: list[AttributionRequirementRecord]
    ) -> bool:
        """Requirements exist -> satisfied only if metadata marks them met."""
        if not reqs:
            return True
        md = record.metadata or {}
        return any("satisfied" in str(v).lower() or "met" in str(v).lower() for v in md.values())

    def _manifest_present(self, item_id: str, org_id: str) -> bool:
        if self._provenance is None:
            return True
        try:
            self._provenance.get_manifest(f"prov-{item_id}", org_id)
            return True
        except ValueError:
            return False


# ----------------------------------------------------------------------
# Router
# ----------------------------------------------------------------------


def _gate_ledger_store() -> RightsLedgerStore:
    from clipos_api.main import database_engine

    return RightsLedgerStore.from_engine(database_engine())


def _gate_provenance_store() -> ProvenanceStore | None:
    from clipos_api.main import database_engine

    return ProvenanceStore.from_engine(database_engine())


router = APIRouter(tags=["rights-gate"])


class _EvaluatePayload(BaseModel):
    """Payload for POST /rights-gate/{item_id}/evaluate."""

    checked_at: datetime | None = Field(default=None, description="Injectable clock; UTC datetime")


@router.post(
    "/rights-gate/{item_id}/evaluate",
    response_model=RightsGateDecision,
    name="evaluate_rights_gate",
)
async def evaluate_rights_gate(
    payload: _EvaluatePayload,
    item_id: Annotated[str, Path(description="The item identifier")],
    org_id: Annotated[str, Query(min_length=1, max_length=64, description="Organization scope")],
    ledger_store: Annotated[RightsLedgerStore, Depends(_gate_ledger_store)],
    provenance_store: Annotated[ProvenanceStore | None, Depends(_gate_provenance_store)],
) -> RightsGateDecision:
    """Evaluate the deterministic rights publication gate."""
    gate = RightsPublicationGate(ledger_store, provenance_store)
    decision = gate.evaluate(
        item_id=item_id,
        org_id=org_id,
        now=payload.checked_at,
        checked_at=payload.checked_at,
    )
    if decision.reason is RightsGateReason.NOT_FOUND:
        raise HTTPException(status_code=404, detail="rights record not found")
    return decision
