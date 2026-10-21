"""Reusable rights-clearance helpers for tests.

The publication gate requires an item whose rights record is CLEAR *and*
whose provenance manifest exists. Both are keyed by the source item id, and
both stores require the creator/source/item entity rows to exist first. This
module performs that sequence over any engine, so a test states its
precondition in one call instead of re-implementing the ordering (and
getting the ordering wrong, which silently produces a denial).

Used by the shared ``clear_rights`` fixture in ``conftest.py`` and directly
by the rights-enforcement tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.engine import Engine

_CREATOR_PREFIX = "creator-"
_SOURCE_PREFIX = "source-"


def clear_rights_for(
    engine: Engine,
    item_id: str,
    *,
    org_id: str = "org-1",
    content_ref: str | None = None,
    record_id: str | None = None,
) -> str:
    """Place *item_id* in the CLEAR rights state and return the record id.

    Registers (idempotently) a creator, a source and the source item, then a
    rights record moved to CLEAR through the real transition path, then one
    provenance step so the manifest the gate looks for exists.

    Creator and source ids are derived from *org_id* so two organizations in
    one test engine never collide: the entity stores reject a re-registration
    whose fields differ, and reject a source that belongs to another org.
    """
    from clipos_api import rights_entities
    from clipos_api.provenance import ProvenanceStep, ProvenanceStore, StepType
    from clipos_api.rights_entities import ContentSource, Creator, SourceItem, SourceType
    from clipos_api.rights_ledger import Basis, RightsLedgerStore, RightsRecord, Status

    creator_id = f"{_CREATOR_PREFIX}{org_id}"
    source_id = f"{_SOURCE_PREFIX}{org_id}"

    entities = rights_entities.RightsEntitiesStore.from_engine(engine)
    entities.register_creator(
        Creator.model_validate(
            {
                "creator_id": creator_id,
                "org_id": org_id,
                "display_name": f"Test Creator {org_id}",
                "contact_ref": f"secrets://contacts/{creator_id}",
            }
        )
    )
    entities.register_source(
        ContentSource.model_validate(
            {
                "source_id": source_id,
                "org_id": org_id,
                "creator_id": creator_id,
                "source_type": SourceType.FEED,
                "canonical_url": f"https://example.com/{source_id}.xml",
            }
        )
    )
    entities.register_item(
        SourceItem.model_validate(
            {
                "item_id": item_id,
                "org_id": org_id,
                "source_id": source_id,
                "external_ref": f"ext:{item_id}",
                "captured_at": datetime(2026, 1, 1, tzinfo=UTC),
                "content_ref": content_ref or f"storage://source-items/{item_id}",
            }
        )
    )

    ledger = RightsLedgerStore.from_engine(engine)
    resolved_record_id = record_id or f"rr-{item_id}"
    ledger.register_record(
        RightsRecord.model_validate(
            {
                "record_id": resolved_record_id,
                "item_id": item_id,
                "org_id": org_id,
                "status": Status.REVIEW_REQUIRED,
                "basis": Basis.OWNED,
                "reviewed_by": "reviewer:audit",
                "metadata": {"source": "test rights clearance"},
            }
        )
    )
    ledger.record_status_change(resolved_record_id, org_id, Status.CLEAR, "reviewer:audit")

    ProvenanceStore.from_engine(engine).append_step(
        item_id,
        org_id,
        ProvenanceStep.model_validate(
            {
                "step_type": StepType.TRANSFORMED,
                "actor_ref": "operator:audit",
                "at": datetime(2026, 1, 2, tzinfo=UTC),
                "inputs_refs": [f"storage://source-items/{item_id}"],
                "outputs_ref": f"storage://renders/{item_id}",
                "transform_note": "test rights clearance",
            }
        ),
    )
    return resolved_record_id
