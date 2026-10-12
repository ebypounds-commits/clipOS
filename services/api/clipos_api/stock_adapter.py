"""Licensed stock/b-roll adapter interface (CLIP-056).

M13 stock footage layer on top of the **existing** M7 rights-entity
chain (CLIP-029/030 `content_sources` + `source_items` — no new tables):
a provider-neutral ``StockClipAdapter`` Protocol plus a deterministic
``MockStockClipAdapter`` that synthesizes candidates from the query hash
— no network, no vendor identifier; providers are addressed exclusively
through the caller-supplied registry alias ``provider_alias``.

``search_stock`` deterministically ensures the creator + PARTNER_FEED
source rows for ``org`` + alias (ids embed the org — creators/sources
key on bare ids, so org-embedding prevents cross-tenant collisions),
then persists each candidate through ``RightsEntitiesStore.register_item``
idempotent on ``(source_id, external_ref)``: a repeat search is a no-op,
changed license/title/dims/content_ref for the same provider ref
conflicts (409). ``content_ref`` is the neutral ref
``stock://{alias}/{provider_ref}``; license evidence lives in item
``metadata`` (license_ref, title, duration, dimensions, provider_alias)
for the M7 rights chain — the CLIP-038 IngestionGate still governs any
later media ingestion (explicitly out of this interface ticket).

Audits: ``stock_searches.completed`` per run (plus the rights-entity
store's own creator/source/item audits). Routes (CLIP-033 pattern):
``POST /stock-searches`` (returns persisted items) and
``GET /stock-items?org_id=&provider_alias=`` (404 when the provider
source has never been registered). No LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha1
from typing import Annotated, Protocol
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.engine import Engine

from clipos_api import audit
from clipos_api.rights_entities import (
    ContentSource,
    Creator,
    RightsEntitiesStore,
    SourceItem,
    SourceType,
)


class StockClipCandidate(BaseModel):
    """Provider-neutral stock candidate returned by an adapter search."""

    model_config = ConfigDict(frozen=True)

    provider_ref: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    license_ref: str = Field(min_length=1, max_length=255)
    duration_ms: int = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)


class StockClipAdapter(Protocol):
    """Provider-neutral licensed stock search backend."""

    def search(self, query: str, provider_alias: str, limit: int) -> list[StockClipCandidate]: ...


class MockStockClipAdapter:
    """Deterministic stand-in: three licensed candidates per query."""

    def search(self, query: str, provider_alias: str, limit: int) -> list[StockClipCandidate]:
        normalized = " ".join(query.split())
        digest = sha1(f"{provider_alias}:{normalized}".encode()).hexdigest()[:8]
        candidates = [
            StockClipCandidate(
                provider_ref=f"stock-{digest}-{index}",
                title=f"{normalized} — take {index + 1}",
                license_ref="CC0-1.0",
                duration_ms=3000 + 1000 * index,
                width=1920,
                height=1080,
            )
            for index in range(3)
        ]
        return candidates[: max(1, min(limit, len(candidates)))]


class StockSearchRequest(BaseModel):
    """Request body for one licensed stock search run."""

    model_config = ConfigDict(frozen=True)

    org_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=200)
    provider_alias: str = Field(min_length=1, max_length=128)
    limit: int = Field(default=5, ge=1, le=10)


class StockClipItem(BaseModel):
    """One persisted licensed stock candidate (source item view)."""

    model_config = ConfigDict(frozen=True)

    source_item_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    org_id: str = Field(min_length=1, max_length=64)
    provider_alias: str = Field(min_length=1, max_length=128)
    provider_ref: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=255)
    license_ref: str = Field(min_length=1, max_length=255)
    duration_ms: int = Field(ge=0)
    width: int = Field(ge=0)
    height: int = Field(ge=0)
    created_at: datetime | None = None


def stock_creator_id(org_id: str, provider_alias: str) -> str:
    """Deterministic org-embedded creator id for one stock provider (pure)."""
    return f"{org_id}:stock-provider:{provider_alias}"[:128]


def stock_source_id(org_id: str, provider_alias: str) -> str:
    """Deterministic org-embedded source id for one stock provider (pure)."""
    return f"{org_id}:stock-feed:{provider_alias}"[:128]


def stock_content_ref(provider_alias: str, provider_ref: str) -> str:
    """Neutral media reference for one stock candidate (pure)."""
    return f"stock://{provider_alias}/{provider_ref}"


def _view_from_item(item: SourceItem, provider_alias: str, source_id: str) -> StockClipItem:
    metadata = item.metadata or {}
    return StockClipItem(
        source_item_id=item.item_id,
        source_id=source_id,
        org_id=item.org_id,
        provider_alias=str(metadata.get("provider_alias", provider_alias)),
        provider_ref=item.external_ref,
        title=str(metadata.get("title", item.external_ref)),
        license_ref=str(metadata.get("license_ref", "")),
        duration_ms=int(metadata.get("duration_ms", 0)),
        width=int(metadata.get("width", 0)),
        height=int(metadata.get("height", 0)),
        created_at=item.created_at,
    )


def search_stock(
    adapter: StockClipAdapter,
    rights: RightsEntitiesStore,
    request: StockSearchRequest,
) -> list[StockClipItem]:
    """Run one licensed stock search and persist results idempotently.

    Ensures the org's stock creator + PARTNER_FEED source rows (deterministic
    org-embedded ids), then registers each candidate on the natural key
    (source_id, external_ref). Identical repeat search -> no-op; changed
    provider metadata for the same ref -> conflict (409). Audits
    ``stock_searches.completed`` with the result count.
    """
    creator_id = stock_creator_id(request.org_id, request.provider_alias)
    source_id = stock_source_id(request.org_id, request.provider_alias)
    rights.register_creator(
        Creator(
            creator_id=creator_id,
            org_id=request.org_id,
            display_name=f"Stock provider {request.provider_alias}",
            contact_ref=f"vault://stock/{request.provider_alias}",
            metadata={"provider_alias": request.provider_alias},
        )
    )
    rights.register_source(
        ContentSource(
            source_id=source_id,
            org_id=request.org_id,
            creator_id=creator_id,
            source_type=SourceType.PARTNER_FEED,
            canonical_url=None,
            metadata={"provider_alias": request.provider_alias},
        )
    )

    candidates = adapter.search(request.query, request.provider_alias, request.limit)
    items: list[StockClipItem] = []
    for candidate in candidates:
        stored = rights.register_item(
            SourceItem(
                item_id=str(uuid4())[:12],
                org_id=request.org_id,
                source_id=source_id,
                external_ref=candidate.provider_ref,
                captured_at=datetime.now(UTC),
                content_ref=stock_content_ref(request.provider_alias, candidate.provider_ref),
                metadata={
                    "provider_alias": request.provider_alias,
                    "license_ref": candidate.license_ref,
                    "title": candidate.title,
                    "duration_ms": candidate.duration_ms,
                    "width": candidate.width,
                    "height": candidate.height,
                },
            )
        )
        items.append(_view_from_item(stored, request.provider_alias, source_id))

    audit.record(
        "stock_searches.completed",
        "stock_searches",
        f"stock_search:{sha1(f'{request.provider_alias}:{request.query}'.encode()).hexdigest()[:12]}",
        {
            "org_id": request.org_id,
            "provider_alias": request.provider_alias,
            "source_id": source_id,
            "query_prefix": request.query[:64],
            "result_count": len(items),
        },
    )
    return items


def list_stock_items(
    rights: RightsEntitiesStore, org_id: str, provider_alias: str
) -> list[StockClipItem]:
    """List persisted candidates for one provider (404 when never searched)."""
    source_id = stock_source_id(org_id, provider_alias)
    stored_items = rights.list_items(source_id, org_id)
    return [_view_from_item(item, provider_alias, source_id) for item in stored_items]


# ---------------------------------------------------------------------------
# Router — Depends providers BEFORE route decorators (CLIP-033 rule)
# ---------------------------------------------------------------------------

router = APIRouter(tags=["stock"])


def _rights(engine: Annotated[Engine, Depends(_database_engine)]) -> RightsEntitiesStore:
    return RightsEntitiesStore.from_engine(engine)


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
    "/stock-searches",
    response_model=list[StockClipItem],
    name="search_stock",
)
def search_stock_route(
    request: StockSearchRequest,
    rights: Annotated[RightsEntitiesStore, Depends(_rights)],
) -> list[StockClipItem]:
    """Run one licensed stock search (Mock adapter; idempotent persist)."""
    try:
        return search_stock(MockStockClipAdapter(), rights, request)
    except ValueError as exc:
        raise _map_error(exc) from exc


@router.get(
    "/stock-items",
    response_model=list[StockClipItem],
    name="list_stock_items",
)
def list_stock_items_route(
    rights: Annotated[RightsEntitiesStore, Depends(_rights)],
    org_id: Annotated[str, Query(min_length=1, max_length=64)],
    provider_alias: Annotated[str, Query(min_length=1, max_length=128)],
) -> list[StockClipItem]:
    """List persisted stock candidates for one provider alias."""
    try:
        return list_stock_items(rights, org_id, provider_alias)
    except ValueError as exc:
        raise _map_error(exc) from exc
