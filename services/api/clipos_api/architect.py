"""Agent Architect proposal generator (CLIP-083).

Deterministic proposal generation over the CLIP-082 model (PRD journey
5 — the Agent Architect turns a recurring weakness observation into a
PROPOSED improvement record with linked evidence):

- **Deterministic identity** — `proposal_id` is derived as
  `gen-{sha1(org|weakness_ref)[:12]}` and each evidence link id as
  `gen-{sha1(proposal_id|evidence_ref)[:12]}`. Generating the same
  weakness twice is therefore a no-op replay: the stored proposal and
  evidence are returned unchanged (external-side-effect idempotency
  without a new table). A *different* title/summary for the same
  weakness conflicts (409) — the identity is the weakness, not the
  wording.
- **Flow** — `generate_proposal` registers the proposal (PROPOSED),
  links every supplied evidence ref (refs only — never content), and
  reports how many evidence links were newly created versus already
  present. The whole run audits `proposal_generation.generated` with
  counts. Existing per-row audits from CLIP-082 still fire for each
  new registration/link.
- **No new table** — generation composes ImprovementStore; nothing is
  persisted except what CLIP-082 already owns. No LLM, no network:
  the "agent" contribution here is the deterministic derivation rule,
  not model inference.

Routes (CLIP-033 pattern): POST /improvement-proposals/generate
(path ordered before /{proposal_id} by FastAPI registration via the
explicit sub-path), returning the proposal plus evidence linkage
counts. GET /improvement-proposals/{proposal_id} still serves fetch.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.experiments import SLUG_PATTERN
from clipos_api.improvement import (
    ImprovementProposal,
    ImprovementStore,
    LinkEvidenceRequest,
    ProposalEvidence,
    RegisterProposalRequest,
)

MAX_GENERATED_EVIDENCE = 16


class GenerateEvidenceItem(BaseModel):
    """One evidence ref to link under the generated proposal."""

    model_config = ConfigDict(frozen=True)

    evidence_ref: str = Field(min_length=1, max_length=512)
    evidence_kind: str = Field(min_length=1, max_length=64, pattern=SLUG_PATTERN)
    note_ref: str | None = Field(default=None, max_length=512)


class GenerateProposalRequest(BaseModel):
    """Request body for one deterministic proposal generation."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    weakness_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(default="", max_length=2000)
    evidence: list[GenerateEvidenceItem] = Field(
        default_factory=list, max_length=MAX_GENERATED_EVIDENCE
    )


class ProposalGeneration(BaseModel):
    """Outcome of one generation run (replay-safe)."""

    model_config = ConfigDict(frozen=True)

    proposal_id: str = Field(min_length=1, max_length=128, pattern=SLUG_PATTERN)
    proposal: ImprovementProposal
    evidence: list[ProposalEvidence] = Field(default_factory=list)
    evidence_created: int = Field(ge=0)
    evidence_replayed: int = Field(ge=0)
    generated_at: datetime


def derive_proposal_id(org_id: str, weakness_ref: str) -> str:
    """Pure deterministic proposal identity from the weakness."""
    digest = sha1(f"{org_id}|{weakness_ref}".encode()).hexdigest()[:12]
    return f"gen-{digest}"


def derive_evidence_id(proposal_id: str, evidence_ref: str) -> str:
    """Pure deterministic evidence identity from proposal + ref."""
    digest = sha1(f"{proposal_id}|{evidence_ref}".encode()).hexdigest()[:12]
    return f"gen-{digest}"


def generate_proposal(
    store: ImprovementStore,
    request: GenerateProposalRequest,
) -> ProposalGeneration:
    """Register (or replay) the proposal and link its evidence.

    Idempotent: identical inputs return the stored proposal with
    evidence_replayed counting the pre-existing links; changed
    title/summary for the same weakness raises (409 at the route).
    """
    proposal_id = derive_proposal_id(request.org_id, request.weakness_ref)
    proposal = store.register_proposal(
        RegisterProposalRequest(
            org_id=request.org_id,
            proposal_id=proposal_id,
            title=request.title,
            weakness_ref=request.weakness_ref,
            summary=request.summary,
        )
    )
    before = {e.evidence_id for e in store.list_evidence(request.org_id, proposal_id)}
    linked: list[ProposalEvidence] = []
    created = 0
    for item in request.evidence:
        evidence_id = derive_evidence_id(proposal_id, item.evidence_ref)
        stored = store.link_evidence(
            LinkEvidenceRequest(
                org_id=request.org_id,
                evidence_id=evidence_id,
                proposal_id=proposal_id,
                evidence_ref=item.evidence_ref,
                evidence_kind=item.evidence_kind,
                note_ref=item.note_ref,
            )
        )
        linked.append(stored)
        if evidence_id not in before:
            created += 1
    replayed = len(linked) - created
    now = datetime.now(UTC)
    audit.record(
        "proposal_generation.generated",
        "improvement_proposals",
        f"improvement_proposal:{proposal_id}",
        {
            "org_id": request.org_id,
            "weakness_ref": request.weakness_ref,
            "evidence_created": created,
            "evidence_replayed": replayed,
        },
    )
    return ProposalGeneration(
        proposal_id=proposal_id,
        proposal=proposal,
        evidence=linked,
        evidence_created=created,
        evidence_replayed=replayed,
        generated_at=now,
    )


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["improvement"])


def _store(engine: Annotated[Engine, Depends(_database_engine)]) -> ImprovementStore:
    return ImprovementStore.from_engine(engine)


def _database_engine() -> Engine:
    from clipos_api.main import database_engine

    return database_engine()


@router.post(
    "/improvement-proposals/generate",
    response_model=ProposalGeneration,
    name="generate_improvement_proposal",
)
def generate_improvement_proposal_route(
    request: GenerateProposalRequest,
    store: Annotated[ImprovementStore, Depends(_store)],
) -> ProposalGeneration:
    """Deterministically register a proposal from a weakness observation.

    Same weakness -> same proposal_id (replay-safe); changed wording
    for an existing identity -> 409.
    """
    try:
        return generate_proposal(store, request)
    except ValueError as exc:
        message = str(exc)
        if "already" in message or "illegal" in message:
            raise HTTPException(status_code=409, detail=message) from exc
        if "not found" in message:
            raise HTTPException(status_code=404, detail=message) from exc
        raise HTTPException(status_code=422, detail=message) from exc
