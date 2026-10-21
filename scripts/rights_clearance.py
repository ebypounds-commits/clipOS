#!/usr/bin/env python3
"""Rights clearance over the HTTP API, shared by the qualification harnesses.

The publication gate is keyed by the rights registry item id and needs BOTH a
CLEAR rights record and a provenance manifest for that item, so the sequence
is: creator -> source -> source item -> rights record -> status transition to
CLEAR -> provenance step. Both stores look the source item up by id, so the
order matters.

Every publication probe in the harnesses clears rights first, so the rule
under test (mode, visibility, public default, kill switch) is the reason the
publication is refused — not an incidental rights refusal. A control that
passes for the wrong reason is not a control.

Item and record ids are used in request paths, so they must be URL-safe: an
id containing "/" is decoded before routing and would never match the route.
The publication therefore names its rights item explicitly with
``rights_item_id`` instead of relying on the ``content_ref`` fallback.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Protocol


class Client(Protocol):
    """The minimal HTTP surface the harnesses rely on."""

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[int, Any]: ...


def clear_rights(
    client: Client,
    org: str,
    item_id: str,
    *,
    walk: Any = None,
    stage: str = "rights",
) -> tuple[bool, str]:
    """Put one item in the CLEAR rights state, over the HTTP API.

    Returns (ok, detail). When *walk* is given the calls are recorded as
    pipeline steps, so the report shows the clearance that made the
    publication possible.
    """
    creator_id = f"qual-creator-{org}"
    source_id = f"qual-source-{org}"
    record_id = f"qual-record-{item_id}"
    record_path = urllib.parse.quote(record_id, safe="")
    item_path = urllib.parse.quote(item_id, safe="")
    calls: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (
            "POST",
            "/creators",
            {
                "creator_id": creator_id,
                "org_id": org,
                "display_name": f"Qualification Creator {org}",
                "contact_ref": f"secrets://contacts/{creator_id}",
            },
        ),
        (
            "POST",
            "/sources",
            {
                "source_id": source_id,
                "org_id": org,
                "creator_id": creator_id,
                "source_type": "feed",
                "canonical_url": f"https://qualification.invalid/{source_id}.xml",
            },
        ),
        (
            "POST",
            "/source-items",
            {
                "item_id": item_id,
                "org_id": org,
                "source_id": source_id,
                "external_ref": f"qual-ext:{item_id}",
                "captured_at": "2026-01-01T00:00:00+00:00",
                "content_ref": f"storage://source-items/{item_id}",
            },
        ),
        (
            "POST",
            "/rights-records",
            {
                "record_id": record_id,
                "item_id": item_id,
                "org_id": org,
                "status": "REVIEW_REQUIRED",
                "basis": "OWNED",
                "reviewed_by": "reviewer:qualification",
                "metadata": {"source": "qualification harness"},
            },
        ),
        (
            "POST",
            f"/rights-records/{record_path}/status",
            {
                "org_id": org,
                "to_status": "CLEAR",
                "reason_ref": "reviewer:qualification",
            },
        ),
        (
            "POST",
            f"/source-items/{item_path}/provenance/steps",
            {
                "org_id": org,
                "step": {
                    "step_type": "transformed",
                    "actor_ref": "operator:qualification",
                    "at": "2026-01-02T00:00:00+00:00",
                    "inputs_refs": [f"storage://source-items/{item_id}"],
                    "outputs_ref": f"storage://renders/{item_id}",
                    "transform_note": "qualification clearance",
                },
            },
        ),
    )
    for index, (method, path, body) in enumerate(calls):
        name = path.strip("/").replace("/", " ")
        if walk is not None:
            walk.step(
                stage,
                f"rights {index + 1}/{len(calls)} {name}",
                method,
                path,
                body,
                expect=(200, 201, 409),
            )
            continue
        status, payload = client.request(method, path, body)
        if status not in (200, 201, 409):
            detail = ""
            if isinstance(payload, dict):
                detail = str(payload.get("detail", ""))[:200]
            return False, f"{method} {path} -> {status} {detail}"
    return True, "rights cleared"
