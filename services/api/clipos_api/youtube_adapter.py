"""YouTube publishing/status adapter (CLIP-059).

Implements the CLIP-058 ``PublishingAdapter`` Protocol for YouTube with a
deterministic Mock. Real YouTube Data API / resumable-upload integration
lands when OAuth credentials and the youtube-dataapi SDK are wired in;
until then the adapter registers a **Mock** under the "youtube" platform
key so the publishing contract is end-to-end testable without any
external call or vendor identifier. Every keyword, capability name, and
status string is a generic platform concept, never a provider ID.

When the real adapter is added later it registers under the same
"youtube" key (overwriting the mock) via
``publishing.register_adapter("youtube", RealYouTubeAdapter())`` — no
adapter-resolution code changes required.

Specification: `08_INTEGRATIONS` §YouTube — Data API for
uploads/metadata/status, resumable upload support, unverified API-project
upload restrictions, channel/account linkage and OAuth scopes.

Audits: ``youtube_adapter.registered`` (one-time startup registration
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


class YouTubePublishingAdapter:
    """Deterministic mock: no network, no youtube-dataapi dependency.

    Simulates a successful upload + immediate published status; real
    adapter replaces this when OAuth credentials are provisioned.
    """

    def health_check(self, account_ref: str) -> AdapterHealth:
        """Mock: always ok with generic YouTube capabilities."""
        return AdapterHealth(
            ok=True,
            capabilities=["upload", "metadata", "status", "resumable"],
            detail="mock YouTube adapter — replace with real Data API adapter",
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        """Mock upload: deterministic external id from the idempotency key."""
        digest = sha1(request.idempotency_key.encode()).hexdigest()[:10]
        return PublishResult(
            external_id=f"yt-mock-{digest}",
            status=PublishStatus.SUCCEEDED,
        )

    def get_status(self, external_id: str) -> PublishStatus:
        """Mock: always published (real adapter polls Data API)."""
        return PublishStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# One-time registration on import
# ---------------------------------------------------------------------------

_register_error: str | None = None

try:
    register_adapter("youtube", YouTubePublishingAdapter())
    audit.record(
        "youtube_adapter.registered",
        "adapters",
        "adapter:youtube",
        {"type": "YouTubePublishingAdapter", "mode": "mock"},
    )
except Exception as exc:  # noqa: BLE001 — startup registration cannot recover
    _register_error = str(exc)


def _ensure_registered() -> None:
    """Raise if adapter registration failed at import time."""
    if _register_error is not None:
        raise RuntimeError(f"YouTube adapter registration failed: {_register_error}")
