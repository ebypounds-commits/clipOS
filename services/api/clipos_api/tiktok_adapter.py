"""TikTok publishing/status adapter (CLIP-060).

Implements the CLIP-058 ``PublishingAdapter`` Protocol for TikTok with a
deterministic Mock. Real TikTok Content Posting API / video-upload
integration lands when OAuth credentials and the tiktok-businessapi SDK
are wired in; until then the adapter registers a **Mock** under the
"tiktok" platform key so the publishing contract is end-to-end testable
without any external call or vendor identifier. Every keyword, capability
name, and status string is a generic platform concept, never a provider
ID.

When the real adapter is added later it registers under the same
"tiktok" key (overwriting the mock) via
``publishing.register_adapter("tiktok", RealTikTokAdapter())`` — no
adapter-resolution code changes required.

Specification: `08_INTEGRATIONS` §TikTok — Content Posting API for
video uploads, privacy/status management, creator account info.

Audits: ``tiktok_adapter.registered`` (one-time startup registration
audit for observability).
"""

from __future__ import annotations

from hashlib import sha1

from clipos_api import audit
from clipos_api.publishing import (
    AdapterHealth,
    PublishRequest,
    PublishResult,
    PublishStatus,
    register_adapter,
)


class TikTokPublishingAdapter:
    """Deterministic mock: no network, no tiktok-businessapi dependency.

    Simulates a successful upload + immediate published status; real
    adapter replaces this when OAuth credentials are provisioned.
    """

    def health_check(self, account_ref: str) -> AdapterHealth:
        """Mock: always ok with generic TikTok capabilities."""
        return AdapterHealth(
            ok=True,
            capabilities=["upload", "privacy", "status", "creator_info"],
            detail="mock TikTok adapter — replace with real Content Posting API adapter",
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        """Mock upload: deterministic external id from the idempotency key."""
        digest = sha1(request.idempotency_key.encode()).hexdigest()[:10]
        return PublishResult(
            external_id=f"tt-mock-{digest}",
            status=PublishStatus.SUCCEEDED,
        )

    def get_status(self, external_id: str) -> PublishStatus:
        """Mock: always published (real adapter polls Content Posting API)."""
        return PublishStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# One-time registration on import
# ---------------------------------------------------------------------------

_register_error: str | None = None

try:
    register_adapter("tiktok", TikTokPublishingAdapter())
    audit.record(
        "tiktok_adapter.registered",
        "adapters",
        "adapter:tiktok",
        {"type": "TikTokPublishingAdapter", "mode": "mock"},
    )
except Exception as exc:  # noqa: BLE001 — startup registration cannot recover
    _register_error = str(exc)


def _ensure_registered() -> None:
    """Raise if adapter registration failed at import time."""
    if _register_error is not None:
        raise RuntimeError(f"TikTok adapter registration failed: {_register_error}")
