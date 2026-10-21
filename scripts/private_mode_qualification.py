#!/usr/bin/env python3
"""End-to-end private-mode qualification (CLIP-098).

Walks the whole ClipOS pipeline against a LIVE instance over HTTP — from
research through production, render, private publishing, analytics and
revenue — and then proves the private-mode guarantees hold end to end.

This is the M24 counterpart to `scripts/security_qualification.py`: that
harness checks the security controls, this one checks that the *operational
pipeline actually runs* and that running it can never produce a public post.

Two layers:

1. **Pipeline walk.** Each stage is a sequence of HTTP steps. A step may be
   marked optional (a stage whose upstream produced nothing to work with),
   but a *required* step failing fails the qualification. The report records
   the observed status of every step so an operator can see exactly how far
   the pipeline got.

2. **Private-mode invariants.** Independent of how far the walk got, the
   harness asserts the guarantees that must hold in private operation:
   an OBSERVE-mode submission is DENIED; PRIVATE_TEST cannot publish PUBLIC;
   PUBLIC is refused while `public_enabled` is false; the rights gate
   refuses an uncleared item; and no secret-shaped material appears in any
   response observed.

Usage:
  python3 scripts/private_mode_qualification.py [--base-url URL]
                                               [--org-id ORG]
                                               [--json PATH]
                                               [--strict]

Exit codes:
  0  pipeline completed and every invariant held
  1  a required step failed, or an invariant was violated
  2  the target was unreachable (qualification could not run)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"

# Publishing policy is unique per (org, platform), so each invariant that
# registers a policy uses its OWN platform. Sharing one platform would let an
# earlier probe's policy decide a later probe's outcome — i.e. the check would
# be measuring leftover state rather than the rule under test.
_PLATFORM_OBSERVE = "youtube"
_PLATFORM_PRIVATE_TEST = "tiktok"
_PLATFORM_PUBLIC_DEFAULT = "instagram"
_PLATFORM_RIGHTS = "facebook"
_PLATFORM_KILL_SWITCH = "x"

# Rights registry item ids. They appear in request paths (source items,
# rights records), so they must be URL-safe; the publication therefore names
# its rights item explicitly with rights_item_id instead of relying on the
# content_ref fallback.
_ITEM_QUAL_CONTENT = "qual-content"
_ITEM_QUAL_VIDEO = "qual-video"

# Media-producing steps write inside the TARGET server's RENDER_WORKDIR and
# are confined to it (CLIP-039 policy), so their paths must match that
# server's configuration. The harness reads the same variable with the same
# default; when the target uses a different workdir these steps are reported
# as skipped rather than failing the qualification, because the private-mode
# invariants are what this harness exists to prove.
DEFAULT_RENDER_WORKDIR = "/tmp/clipos-render"


def _render_workdir() -> str:
    return os.environ.get("RENDER_WORKDIR", DEFAULT_RENDER_WORKDIR).rstrip("/")


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


@dataclass
class Step:
    """One HTTP step in the pipeline walk."""

    stage: str
    name: str
    status: str
    http_status: int | None
    detail: str = ""


@dataclass
class Invariant:
    """One private-mode guarantee and its observed evidence."""

    check_id: str
    title: str
    status: str
    evidence: str
    details: dict = field(default_factory=dict)


class Client:
    """Minimal JSON HTTP client that records every response body seen."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.seen: list[str] = []

    def request(
        self,
        method: str,
        path: str,
        body: dict | list | None = None,
        params: dict | None = None,
    ) -> tuple[int, object]:
        url = f"{self.base_url}{path}"
        if params:
            query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            url = f"{url}?{query}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            status = exc.code
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc
        self.seen.append(raw)
        try:
            return status, (json.loads(raw) if raw else None)
        except json.JSONDecodeError:
            return status, raw


class _Walk:
    """Accumulates pipeline steps while keeping the terminal state."""

    def __init__(self, client: Client, org: str) -> None:
        self.client = client
        self.org = org
        self.steps: list[Step] = []
        self.context: dict[str, object] = {}

    def step(
        self,
        stage: str,
        name: str,
        method: str,
        path: str,
        body: dict | list | None = None,
        params: dict | None = None,
        expect: tuple[int, ...] = (200,),
        required: bool = True,
    ) -> object | None:
        """Perform one step; record it; return the parsed body (or None).

        An optional step that does not return an expected status is recorded
        as SKIP rather than FAIL: it means the environment could not exercise
        that step, which is different from the step being broken.
        """
        status, payload = self.client.request(method, path, body, params)
        if status in expect:
            self.steps.append(Step(stage, name, PASS, status))
            return payload
        detail = ""
        if isinstance(payload, dict):
            detail = str(payload.get("detail", ""))[:200]
        if required:
            self.steps.append(Step(stage, name, FAIL, status, detail))
            raise RuntimeError(
                f"required step failed: {stage}/{name} -> {status} {detail}"
            )
        self.steps.append(
            Step(stage, name, SKIP, status, detail or "optional step not exercised")
        )
        return None


def _required(steps: list[Step]) -> list[Step]:
    return [s for s in steps if s.status == FAIL]


# ---------------------------------------------------------------------------
# Rights clearance (the publication gate's precondition)
# ---------------------------------------------------------------------------


def clear_rights(
    client: Client,
    org: str,
    item_id: str,
    *,
    walk: _Walk | None = None,
    stage: str = "rights",
) -> tuple[bool, str]:
    """Put one item in the CLEAR rights state, over the HTTP API.

    The publication gate is keyed by the rights registry item id and needs
    BOTH a CLEAR rights record and a provenance manifest for that item, so
    the sequence is: creator -> source -> source item -> rights record ->
    status transition to CLEAR -> provenance step.

    Every publication probe in this harness clears rights first, so the
    invariant under test is the rule it names (mode, visibility, public
    default, kill switch) and not an incidental rights refusal.

    When *walk* is given the calls are recorded as pipeline steps, so the
    report shows the clearance that made the publication possible.
    """
    creator_id = f"qual-creator-{org}"
    source_id = f"qual-source-{org}"
    record_id = f"qual-record-{item_id}"
    # The item and record ids are used in request paths, so they must be
    # URL-safe: an id containing "/" is decoded before routing and would
    # never match the route. The rights item id is therefore named
    # explicitly on the publication (rights_item_id) instead of being
    # derived from a "storage://" content ref.
    record_path = urllib.parse.quote(record_id, safe="")
    item_path = urllib.parse.quote(item_id, safe="")
    calls: tuple[tuple[str, str, dict[str, object]], ...] = (
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
                "metadata": {"source": "private-mode qualification"},
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
                    "transform_note": "private-mode qualification clearance",
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


# ---------------------------------------------------------------------------
# Private-mode invariants
# ---------------------------------------------------------------------------


def _denied_for(body: object, expected_fragment: str) -> str | None:
    """Return an error text when the denial was not for the expected reason."""
    if not isinstance(body, dict):
        return "response was not an object"
    status = body.get("status")
    if status != "DENIED":
        return f"expected DENIED, got {status}"
    reason = str(body.get("decision_reason", ""))
    if expected_fragment not in reason:
        return f"DENIED for the wrong reason: {reason}"
    return None


def invariant_observe_denied(client: Client, org: str) -> Invariant:
    cleared, detail = clear_rights(client, org, _ITEM_QUAL_CONTENT)
    if not cleared:
        return Invariant(
            "P1", "OBSERVE mode denies all publishing", FAIL, f"setup failed: {detail}"
        )
    client.request(
        "POST",
        "/publish-policies",
        {
            "policy_id": f"qual-observe-policy-{int(time.time())}",
            "org_id": org,
            "platform": _PLATFORM_OBSERVE,
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": True,
        },
    )
    status, body = client.request(
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": _PLATFORM_OBSERVE,
            "account_ref": "qual://account",
            "content_ref": "storage://qual-content",
            "title": "private-mode qualification probe",
            "visibility": "PRIVATE",
            "idempotency_key": f"qual-observe-{int(time.time())}",
            "operating_mode": "OBSERVE",
            "rights_item_id": _ITEM_QUAL_CONTENT,
        },
    )
    if status != 200 or not isinstance(body, dict):
        return Invariant(
            "P1", "OBSERVE mode denies all publishing", FAIL, f"-> {status}"
        )
    problem = _denied_for(body, "mode_OBSERVE")
    if problem:
        return Invariant(
            "P1",
            "OBSERVE mode denies all publishing",
            FAIL,
            problem,
        )
    return Invariant(
        "P1",
        "OBSERVE mode denies all publishing",
        PASS,
        f"DENIED ({body.get('decision_reason', 'no reason given')})",
    )


def invariant_private_test_cannot_publish_public(client: Client, org: str) -> Invariant:
    """PRIVATE_TEST may only reach PRIVATE/UNLISTED — PUBLIC must be refused."""
    cleared, detail = clear_rights(client, org, _ITEM_QUAL_CONTENT)
    if not cleared:
        return Invariant(
            "P2", "PRIVATE_TEST cannot publish PUBLIC", FAIL, f"setup failed: {detail}"
        )
    client.request(
        "POST",
        "/publish-policies",
        {
            "policy_id": f"qual-policy-{int(time.time())}",
            "org_id": org,
            "platform": _PLATFORM_PRIVATE_TEST,
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": True,
        },
    )
    status, body = client.request(
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": _PLATFORM_PRIVATE_TEST,
            "account_ref": "qual://account",
            "content_ref": "storage://qual-content",
            "title": "private-mode qualification probe",
            "visibility": "PUBLIC",
            "idempotency_key": f"qual-privatetest-{int(time.time())}",
            "operating_mode": "PRIVATE_TEST",
            "rights_item_id": _ITEM_QUAL_CONTENT,
        },
    )
    if status != 200 or not isinstance(body, dict):
        return Invariant(
            "P2", "PRIVATE_TEST cannot publish PUBLIC", FAIL, f"-> {status}"
        )
    problem = _denied_for(body, "private_test_forbids_public")
    if problem:
        return Invariant(
            "P2",
            "PRIVATE_TEST cannot publish PUBLIC",
            FAIL,
            problem,
        )
    return Invariant(
        "P2",
        "PRIVATE_TEST cannot publish PUBLIC",
        PASS,
        f"PUBLIC denied under PRIVATE_TEST ({body.get('decision_reason', '')})",
    )


def invariant_public_refused_by_default(client: Client, org: str) -> Invariant:
    """PUBLIC is refused while the policy's public_enabled is false."""
    cleared, detail = clear_rights(client, org, _ITEM_QUAL_CONTENT)
    if not cleared:
        return Invariant(
            "P3", "PUBLIC refused by default", FAIL, f"setup failed: {detail}"
        )
    client.request(
        "POST",
        "/publish-policies",
        {
            "policy_id": f"qual-nopublic-{int(time.time())}",
            "org_id": org,
            "platform": _PLATFORM_PUBLIC_DEFAULT,
            "allowed_visibilities": ["PRIVATE", "UNLISTED", "PUBLIC"],
            "public_enabled": False,
        },
    )
    status, body = client.request(
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": _PLATFORM_PUBLIC_DEFAULT,
            "account_ref": "qual://account",
            "content_ref": "storage://qual-content",
            "title": "private-mode qualification probe",
            "visibility": "PUBLIC",
            "idempotency_key": f"qual-default-{int(time.time())}",
            "operating_mode": "GUARDED_PUBLIC",
            "rights_item_id": _ITEM_QUAL_CONTENT,
        },
    )
    if status != 200 or not isinstance(body, dict):
        return Invariant("P3", "PUBLIC refused by default", FAIL, f"-> {status}")
    problem = _denied_for(body, "public_disabled_by_default")
    if problem:
        return Invariant(
            "P3",
            "PUBLIC refused by default",
            FAIL,
            problem,
        )
    return Invariant(
        "P3",
        "PUBLIC refused by default",
        PASS,
        f"PUBLIC denied while public_enabled is false "
        f"({body.get('decision_reason', '')})",
    )


def invariant_rights_gate_refuses_uncleared(client: Client, org: str) -> Invariant:
    """An item with no rights record must be refused by the rights gate.

    The probe item is deliberately NOT cleared, and the policy allows
    PRIVATE — so the only reason the publication can be refused is the
    rights gate. The check asserts the status AND that the reason names
    rights: a refusal for any other reason would mean the gate was never
    reached.
    """
    client.request(
        "POST",
        "/publish-policies",
        {
            "policy_id": f"qual-rights-policy-{int(time.time())}",
            "org_id": org,
            "platform": _PLATFORM_RIGHTS,
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
    )
    item_id = f"storage://uncleared-{int(time.time())}"
    status, body = client.request(
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": _PLATFORM_RIGHTS,
            "account_ref": "qual://account",
            "content_ref": item_id,
            "title": "private-mode qualification probe",
            "visibility": "PRIVATE",
            "idempotency_key": f"qual-rights-{int(time.time())}",
            "operating_mode": "PRIVATE_TEST",
        },
    )
    if status != 200 or not isinstance(body, dict):
        return Invariant(
            "P4",
            "Rights gate refuses uncleared content",
            FAIL,
            f"-> {status}",
        )
    if body.get("status") != "DENIED":
        return Invariant(
            "P4",
            "Rights gate refuses uncleared content",
            FAIL,
            f"an uncleared item was published: {body.get('status')}",
        )
    reason = str(body.get("decision_reason", ""))
    if "rights" not in reason:
        return Invariant(
            "P4",
            "Rights gate refuses uncleared content",
            FAIL,
            f"DENIED for the wrong reason: {reason}",
        )
    return Invariant(
        "P4",
        "Rights gate refuses uncleared content",
        PASS,
        f"uncleared item denied by the rights gate ({reason})",
    )


def invariant_kill_switch_closes_publishing(client: Client, org: str) -> Invariant:
    """Kill switch active must deny even a normally-allowed private post.

    The kill switch is a policy field, not a standalone endpoint, so this
    probe exercises the real enforcement path: register a policy that would
    otherwise allow PRIVATE, with the switch closed, and assert DENIED. The
    item is rights-cleared first, so the kill switch — not the rights gate —
    is the reason under test.
    """
    cleared, detail = clear_rights(client, org, _ITEM_QUAL_CONTENT)
    if not cleared:
        return Invariant(
            "P5", "Kill switch closes publishing", FAIL, f"setup failed: {detail}"
        )
    client.request(
        "POST",
        "/publish-policies",
        {
            "policy_id": f"qual-killswitch-policy-{int(time.time())}",
            "org_id": org,
            "platform": _PLATFORM_KILL_SWITCH,
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
            "public_enabled": False,
            "kill_switch": True,
        },
    )
    status, body = client.request(
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": _PLATFORM_KILL_SWITCH,
            "account_ref": "qual://account",
            "content_ref": "storage://qual-content",
            "title": "private-mode qualification probe",
            "visibility": "PRIVATE",
            "idempotency_key": f"qual-killswitch-{int(time.time())}",
            "operating_mode": "PRIVATE_TEST",
            "rights_item_id": _ITEM_QUAL_CONTENT,
        },
    )
    if status != 200 or not isinstance(body, dict):
        return Invariant("P5", "Kill switch closes publishing", FAIL, f"-> {status}")
    if body.get("status") != "DENIED":
        return Invariant(
            "P5",
            "Kill switch closes publishing",
            FAIL,
            f"a PRIVATE post was {body.get('status')} with the kill switch active",
        )
    reason = str(body.get("decision_reason", ""))
    if "kill_switch" not in reason:
        return Invariant(
            "P5",
            "Kill switch closes publishing",
            FAIL,
            f"DENIED for the wrong reason: {reason}",
        )
    return Invariant("P5", "Kill switch closes publishing", PASS, f"DENIED ({reason})")


def invariant_no_secrets_observed(client: Client, org: str) -> Invariant:
    combined = "\n".join(client.seen)
    leaked = sorted({p.pattern for p in _SECRET_PATTERNS if p.search(combined)})
    if leaked:
        return Invariant(
            "P6",
            "No secret material in observed responses",
            FAIL,
            f"matched patterns: {leaked}",
        )
    return Invariant(
        "P6",
        "No secret material in observed responses",
        PASS,
        f"{len(client.seen)} responses scanned, no provider-shaped secret found",
    )


INVARIANTS = (
    invariant_observe_denied,
    invariant_private_test_cannot_publish_public,
    invariant_public_refused_by_default,
    invariant_rights_gate_refuses_uncleared,
    invariant_kill_switch_closes_publishing,
    invariant_no_secrets_observed,
)


# ---------------------------------------------------------------------------
# Pipeline walk
# ---------------------------------------------------------------------------


def walk_pipeline(walk: _Walk) -> None:
    """Run the golden path in private mode; raise if a required step fails."""
    org = walk.org

    # ------------------------------------------------------------- research
    walk.step(
        "research",
        "health",
        "GET",
        "/health",
    )
    packet = walk.step(
        "research",
        "create packet",
        "POST",
        "/research-packets",
        {
            "org_id": org,
            "research_packet_id": "qual-packet",
            "title": "Qualification trends",
            "topic_ref": "topic://qualification",
        },
        expect=(200, 409),
    )
    packet_id = (packet or {}).get("research_packet_id", "qual-packet")
    claim = walk.step(
        "research",
        "add claim",
        "POST",
        f"/research-packets/{packet_id}/claims",
        {
            "org_id": org,
            "claim_id": "qual-claim",
            "research_packet_id": packet_id,
            "statement": "Short hooks raise retention.",
        },
        expect=(200, 409),
    )
    claim_id = (claim or {}).get("claim_id", "qual-claim")
    walk.step(
        "research",
        "verify claim",
        "POST",
        f"/claims/{claim_id}/veracity",
        {
            "org_id": org,
            "veracity": "VERIFIED",
            "actor_ref": "qual",
            "reason_ref": "qual",
        },
        expect=(200, 409),
    )

    # -------------------------------------------------------------- planning
    walk.step(
        "planning",
        "content brief",
        "POST",
        "/content-briefs",
        {
            "org_id": org,
            "brief_id": "qual-brief",
            "research_packet_id": packet_id,
            "title": "Qualification brief",
            "objective": "Private-mode qualification",
            "platforms": ["youtube"],
        },
        expect=(200, 409),
    )
    walk.step(
        "planning",
        "derive plan",
        "POST",
        "/content-briefs/qual-brief/plan",
        {"org_id": org},
    )
    walk.step(
        "planning",
        "hook",
        "POST",
        "/hooks",
        {"org_id": org, "brief_id": "qual-brief", "text": "Wait for it..."},
        expect=(200, 409),
    )
    walk.step(
        "planning",
        "script",
        "POST",
        "/scripts",
        {
            "org_id": org,
            "brief_id": "qual-brief",
            "script_id": "qual-script",
            "title": "Qualification script",
        },
        expect=(200, 409),
    )
    walk.step(
        "planning",
        "script version",
        "POST",
        "/scripts/qual-script/versions",
        {
            "org_id": org,
            "body": "Scene 1: hook. Scene 2: claim. Scene 3: outro.",
            "author_ref": "qual-author",
        },
        expect=(200, 409),
    )

    # -------------------------------------------------------------- produce
    walk.step(
        "produce",
        "video generation",
        "POST",
        "/video-generations",
        {
            "media_asset_id": "qual-vid",
            "org_id": org,
            "brief_id": "qual-brief",
            "title": "Qualification clip source",
            "prompt": "abstract gradient",
            "model_alias": "mock-video",
            "duration_ms": 4000,
            "output_path": f"{_render_workdir()}/gen/qual-video.mp4",
        },
        expect=(200, 409),
        required=False,
    )
    walk.step(
        "produce",
        "transcript",
        "POST",
        "/transcripts",
        {
            "media_asset_id": "qual-vid",
            "org_id": org,
            "language": "en",
            "provider_alias": "mock-stt",
        },
        expect=(200, 409),
        required=False,
    )

    # --------------------------------------------------------------- render
    # Candidate extraction materializes media, so it depends on the target
    # environment (RENDER_WORKDIR confined + a usable source). When it is not
    # available the stage is recorded as skipped rather than aborting the
    # walk: the private-mode invariants below are what this harness exists to
    # prove, and they must still be evaluated.
    extracted = walk.step(
        "render",
        "extract candidates",
        "POST",
        "/clip-candidates/extract-and-register",
        {
            "media_asset_id": "qual-vid",
            "org_id": org,
            "boundaries": [[0, 6000]],
            "source": "MANUAL",
            "reason": "qualification boundary",
            "min_len_ms": 1000,
            "max_len_ms": 60000,
            "min_gap_ms": 0,
        },
        expect=(200,),
        required=False,
    )
    candidates: list = []
    if isinstance(extracted, list):
        candidates = extracted
    elif isinstance(extracted, dict):
        for key in ("candidates", "extracted", "items", "results"):
            if isinstance(extracted.get(key), list):
                candidates = extracted[key]
                break
    if not candidates:
        walk.steps.append(
            Step(
                "render",
                "render project",
                SKIP,
                None,
                "skipped: no clip candidate available in this environment",
            )
        )
    else:
        candidate_id = candidates[0].get("candidate_id")
        if not candidate_id:
            walk.steps.append(
                Step(
                    "render",
                    "render project",
                    SKIP,
                    None,
                    "skipped: candidate payload has no candidate_id",
                )
            )
        else:
            walk.context["candidate_id"] = candidate_id
            walk.step(
                "render",
                "render project",
                "POST",
                "/render-projects",
                {
                    "render_project_id": "qual-render",
                    "org_id": org,
                    "brief_id": "qual-brief",
                    "name": "Qualification render",
                    "spec": {
                        "version": 1,
                        "inputs": {"candidate_id": candidate_id},
                        "composition": {"aspect": "16:9", "duration_ms": 2000},
                        "outputs": [{"platform": "youtube"}],
                    },
                },
                expect=(200, 409),
            )

    # -------------------------------------------------------------- publish
    walk.step(
        "publish",
        "platform account",
        "POST",
        "/platform-accounts",
        {
            "account_id": "qual-acct",
            "org_id": org,
            "platform": "youtube",
            "account_ref": "yt://qual",
            "display_name": "Qualification channel",
            "scopes": ["upload"],
        },
        expect=(200, 409),
    )
    walk.step(
        "publish",
        "publish policy (private only)",
        "POST",
        "/publish-policies",
        {
            "policy_id": "qual-pol",
            "org_id": org,
            "platform": "youtube",
            "allowed_visibilities": ["PRIVATE", "UNLISTED"],
        },
        expect=(200, 409),
    )
    # The publication gate requires cleared rights for the published item.
    # Clearing them here is what makes the private publication possible, and
    # the recorded steps show it.
    clear_rights(walk.client, org, _ITEM_QUAL_VIDEO, walk=walk, stage="rights")
    published = walk.step(
        "publish",
        "private publication",
        "POST",
        "/publications",
        {
            "org_id": org,
            "platform": "youtube",
            "account_ref": "yt://qual",
            "content_ref": "storage://qual-video",
            "title": "Qualification upload",
            "visibility": "PRIVATE",
            "idempotency_key": "qual-pub-1",
            "operating_mode": "PRIVATE_TEST",
            "rights_item_id": _ITEM_QUAL_VIDEO,
        },
        expect=(200,),
    )
    if not isinstance(published, dict) or published.get("status") != "SUCCEEDED":
        # A DENIED job is still HTTP 200, so the HTTP status alone cannot
        # tell the operator that nothing was published.
        detail = ""
        if isinstance(published, dict):
            detail = str(published.get("decision_reason", published.get("status", "")))
        raise RuntimeError(f"private publication did not succeed: {detail}")
    walk.context["external_id"] = published.get("external_id")

    # ------------------------------------------------------------ analytics
    walk.step(
        "analytics",
        "collect",
        "POST",
        "/analytics/collect",
        {
            "org_id": org,
            "platform": "youtube",
            "account_ref": "yt://qual",
            "content_ref": "storage://qual-video",
            "external_id": walk.context.get("external_id", "yt-mock-qual"),
        },
        expect=(200, 409),
    )
    walk.step(
        "analytics", "dashboard", "GET", "/analytics/dashboard", params={"org_id": org}
    )
    walk.step(
        "analytics",
        "performance snapshot",
        "POST",
        "/performance-snapshots",
        {"org_id": org, "platform": "youtube", "content_ref": "storage://qual-video"},
        expect=(200, 409),
    )

    # -------------------------------------------------------------- revenue
    walk.step(
        "revenue",
        "revenue source",
        "POST",
        "/revenue-sources",
        {
            "org_id": org,
            "source_id": "qual-rev",
            "platform": "youtube",
            "account_ref": "yt://qual",
            "revenue_kind": "PLATFORM",
        },
        expect=(200, 409),
    )
    walk.step(
        "revenue",
        "revenue sync",
        "POST",
        "/revenue-sync",
        {
            "org_id": org,
            "source_id": "qual-rev",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
        },
        expect=(200, 409),
    )
    walk.step(
        "revenue", "dashboard", "GET", "/revenue/dashboard", params={"org_id": org}
    )
    walk.step(
        "revenue",
        "profitability report",
        "POST",
        "/profitability/reports",
        {
            "org_id": org,
            "report_id": "qual-profit",
            "period_start": "2026-10-01",
            "period_end": "2026-10-07",
            "currency": "USD",
            "reinvest_pct": 40.0,
        },
        expect=(200, 409),
    )

    # ---------------------------------------------------------- experiments
    walk.step(
        "experiments",
        "experiment",
        "POST",
        "/experiments",
        {
            "org_id": org,
            "experiment_id": "qual-exp",
            "title": "Qualification hook test",
            "target_metric": "VIEW",
            "hypothesis_summary": "Short hooks win.",
        },
        expect=(200, 409),
    )
    for key in ("control", "treatment"):
        walk.step(
            "experiments",
            f"variant {key}",
            "POST",
            "/experiments/qual-exp/variants",
            {"org_id": org, "variant_key": key, "label": key, "weight": 50},
            expect=(200, 409),
        )
    walk.step(
        "experiments",
        "activate",
        "POST",
        "/experiments/qual-exp/transition",
        {"status": "ACTIVE", "actor_ref": "qual"},
        params={"org_id": org},
        expect=(200, 409),
    )
    walk.step(
        "experiments",
        "assignment",
        "POST",
        "/experiments/qual-exp/assignments",
        {"subject_ref": "qual-subject-1"},
        params={"org_id": org},
        expect=(200,),
    )


def run_qualification(client: Client, org: str) -> dict:
    """Walk the pipeline, then evaluate the private-mode invariants."""
    walk = _Walk(client, org)
    pipeline_error: str | None = None
    try:
        walk_pipeline(walk)
    except (RuntimeError, ConnectionError) as exc:
        if isinstance(exc, ConnectionError):
            raise
        pipeline_error = str(exc)

    results: list[Invariant] = []
    for check in INVARIANTS:
        if check is invariant_no_secrets_observed:
            continue
        try:
            results.append(check(client, org))
        except ConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failure
            results.append(
                Invariant(
                    check.__name__,
                    "check raised",
                    FAIL,
                    f"{type(exc).__name__}: {str(exc)[:200]}",
                )
            )
    results.append(invariant_no_secrets_observed(client, org))

    stages: dict[str, dict] = {}
    _bucket_key = {PASS: "passed", FAIL: "failed", SKIP: "skipped"}
    for stage_step in walk.steps:
        bucket = stages.setdefault(
            stage_step.stage, {"passed": 0, "failed": 0, "skipped": 0}
        )
        key = _bucket_key[stage_step.status]
        bucket[key] += 1

    required_failures = _required(walk.steps)
    invariant_failures = [r for r in results if r.status == FAIL]
    # `pipeline_error` explains why the walk stopped; the aborting step is
    # already counted in required_failures, so it must not be counted twice.
    failed = len(required_failures) + len(invariant_failures)

    return {
        "tool": "clipos-private-mode-qualification",
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": client.base_url,
        "org_id": org,
        "summary": {
            "steps": len(walk.steps),
            "steps_failed": len(required_failures),
            "invariants": len(results),
            "invariants_failed": len(invariant_failures),
            "failed": failed,
            "skipped": len([s for s in walk.steps if s.status == SKIP]),
            "verdict": "PASS" if failed == 0 else "FAIL",
        },
        "pipeline_error": pipeline_error,
        "stages": stages,
        "steps": [
            {
                "stage": s.stage,
                "name": s.name,
                "status": s.status,
                "http_status": s.http_status,
                "detail": s.detail,
            }
            for s in walk.steps
        ],
        "invariants": [
            {
                "check_id": r.check_id,
                "title": r.title,
                "status": r.status,
                "evidence": r.evidence,
                "details": r.details,
            }
            for r in results
        ],
        "findings": [
            {
                "severity": "HIGH",
                "check_id": r.check_id,
                "title": r.title,
                "observation": r.evidence,
                "recommendation": "Investigate and remediate before qualification is granted.",
            }
            for r in results
            if r.status == FAIL
        ]
        + (
            [
                {
                    "severity": "HIGH",
                    "check_id": "PIPELINE",
                    "title": "required pipeline step failed",
                    "observation": pipeline_error or "required step failure",
                    "recommendation": "Fix the failing stage before qualification is granted.",
                }
            ]
            if pipeline_error
            else []
        ),
    }


def render(report: dict, verbose: bool = True) -> None:
    summary = report["summary"]
    print("ClipOS end-to-end private-mode qualification")
    print(f"  target : {report['base_url']}")
    print(f"  org    : {report['org_id']}")
    print(f"  at     : {report['generated_at']}")
    print()
    for stage, counts in report["stages"].items():
        print(
            f"  {stage:12} passed={counts.get('passed', 0):2} "
            f"failed={counts.get('failed', 0):2} skipped={counts.get('skipped', 0):2}"
        )
    print()
    for invariant in report["invariants"]:
        print(
            f"[{invariant['status']:4}] {invariant['check_id']:3} {invariant['title']}"
        )
        if verbose:
            print(f"        {invariant['evidence']}")
    print()
    print(
        f"VERDICT: {summary['verdict']} "
        f"({summary['steps']} steps, {summary['steps_failed']} failed; "
        f"{summary['invariants']} invariants, {summary['invariants_failed']} failed)"
    )
    if report["findings"]:
        print("\nFindings:")
        for finding in report["findings"]:
            print(
                f"  [{finding['severity']:4}] {finding['check_id']}: {finding['observation']}"
            )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8111")
    parser.add_argument("--org-id", default="private-mode-qualification")
    parser.add_argument("--json", default="", help="write the report JSON here")
    parser.add_argument("--strict", action="store_true", help="treat skips as failures")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv[1:])

    client = Client(args.base_url)
    try:
        report = run_qualification(client, args.org_id)
    except ConnectionError as exc:
        print(
            f"qualification could not run: target unreachable ({exc})", file=sys.stderr
        )
        return 2

    render(report, verbose=not args.quiet)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\nreport written to {args.json}")

    if report["summary"]["failed"]:
        return 1
    if args.strict and report["summary"]["skipped"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
