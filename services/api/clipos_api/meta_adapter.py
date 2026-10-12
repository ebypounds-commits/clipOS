"""Meta (Facebook/Instagram) publishing/status adapter (CLIP-061).

Implements the CLIP-058 ``PublishingAdapter`` Protocol for Meta platforms
with a deterministic Mock. Real Meta Graph API / video-upload integration
lands when App credentials and the facebook-business SDK are wired in;
until then the adapter registers a **Mock** under the "meta" platform
key so the publishing contract is end-to-end testable without any
external call or vendor identifier. Every keyword, capability name, and
status string is a generic platform concept, never a provider ID.

When the real adapter is added later it registers under the same
"meta" key (overwriting the mock) via
``publishing.register_adapter("meta", RealMetaAdapter())`` — no
adapter-resolution code changes required.

Specification: `08_INTEGRATIONS` §Meta — Graph API for video posts,
page/IG account publishing, privacy/status management.

Verification note: Meta developer documentation returned HTTP 400 at
time of implementation (2026-10-12). Real adapter integration requires
current-doc verification when credentials are provisioned. Mock-only
implementation does not block per ticket acceptance criteria.

Audits: ``meta_adapter.registered`` (one-time startup registration
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


class MetaPublishingAdapter:
    """Deterministic mock: no network, no facebook-business dependency.

    Simulates a successful upload + immediate published status; real
    adapter replaces this when App credentials are provisioned.
    """

    def health_check(self, account_ref: str) -> AdapterHealth:
        """Mock: always ok with generic Meta capabilities."""
        return AdapterHealth(
            ok=True,
            capabilities=["upload", "page_publish", "status", "privacy"],
            detail="mock Meta adapter — replace with real Graph API adapter",
        )

    def publish(self, request: PublishRequest) -> PublishResult:
        """Mock upload: deterministic external id from the idempotency key."""
        digest = sha1(request.idempotency_key.encode()).hexdigest()[:10]
        return PublishResult(
            external_id=f"meta-mock-{digest}",
            status=PublishStatus.SUCCEEDED,
        )

    def get_status(self, external_id: str) -> PublishStatus:
        """Mock: always published (real adapter polls Graph API)."""
        return PublishStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# One-time registration on import
# ---------------------------------------------------------------------------

_register_error: str | None = None

try:
    register_adapter("meta", MetaPublishingAdapter())
    audit.record(
        "meta_adapter.registered",
        "adapters",
        "adapter:meta",
        {"type": "MetaPublishingAdapter", "mode": "mock"},
    )
except Exception as exc:  # noqa: BLE001 — startup registration cannot recover
    _register_error = str(exc)


def _ensure_registered() -> None:
    """Raise if adapter registration failed at import time."""
    if _register_error is not None:
        raise RuntimeError(f"Meta adapter registration failed: {_register_error}")
