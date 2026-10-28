"""Contract tests for the provider-neutral interfaces (CLIP-094 protocol policy).

``clipos_api.providers`` declares the boundary every concrete adapter must
satisfy. The protocols are ``runtime_checkable``, so an object can be tested
against them at runtime, and each protocol body raises ``NotImplementedError``
so an adapter that forgets to implement a method fails loudly instead of
silently returning ``None``.

These tests pin both properties for every protocol in the module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clipos_api import providers

# (protocol, method name, keyword arguments the protocol method accepts)
_PROTOCOL_CALLS: tuple[tuple[Any, str, dict[str, Any]], ...] = (
    (providers.LLMProvider, "complete", {"alias": "a", "prompt": "p", "options": {}}),
    (providers.EmbeddingProvider, "embed", {"alias": "a", "inputs": ["x"], "options": {}}),
    (
        providers.TranscriptionProvider,
        "transcribe",
        {"alias": "a", "media_ref": "s3://x", "options": {}},
    ),
    (
        providers.TextToSpeechProvider,
        "synthesize",
        {"alias": "a", "text": "hello", "options": {}},
    ),
    (
        providers.ImageGenerationProvider,
        "generate_image",
        {"alias": "a", "prompt": "p", "options": {}},
    ),
    (
        providers.VideoGenerationProvider,
        "generate_video",
        {"alias": "a", "prompt": "p", "options": {}},
    ),
    (providers.SearchProvider, "search", {"alias": "a", "query": "q", "options": {}}),
    (
        providers.PublishingProvider,
        "publish",
        {"alias": "a", "payload": {}, "idempotency_key": "k"},
    ),
    (providers.AnalyticsProvider, "fetch_metrics", {"alias": "a", "query": {}}),
    (providers.AffiliateProvider, "list_offers", {"alias": "a", "query": {}}),
    (
        providers.StorageProvider,
        "put_object",
        {"alias": "a", "key": "k", "data": b"x", "content_type": "text/plain"},
    ),
    (providers.StorageProvider, "get_object", {"alias": "a", "key": "k"}),
)


@pytest.mark.parametrize(("protocol", "method_name", "kwargs"), _PROTOCOL_CALLS)
def test_protocol_default_implementation_refuses_to_fabricate(
    protocol: Any, method_name: str, kwargs: dict[str, Any]
) -> None:
    """The protocol body must raise instead of returning a placeholder value."""

    def _invoke() -> Any:
        return getattr(protocol, method_name)(None, **kwargs)

    with pytest.raises(NotImplementedError):
        asyncio.run(_invoke())


class _ConformingLLM:
    async def complete(self, *, alias: str, prompt: str, options: Any) -> Any:
        return {"alias": alias, "prompt": prompt, "options": options}


class _ConformingStorage:
    async def put_object(self, *, alias: str, key: str, data: bytes, content_type: str) -> Any:
        return {"key": key}

    async def get_object(self, *, alias: str, key: str) -> bytes:
        return b""


class _Empty:
    pass


def test_llm_provider_is_runtime_checkable() -> None:
    assert isinstance(_ConformingLLM(), providers.LLMProvider)
    assert not isinstance(_Empty(), providers.LLMProvider)


def test_storage_provider_is_runtime_checkable() -> None:
    assert isinstance(_ConformingStorage(), providers.StorageProvider)
    assert not isinstance(_Empty(), providers.StorageProvider)


@pytest.mark.parametrize(
    "protocol",
    [
        providers.LLMProvider,
        providers.EmbeddingProvider,
        providers.TranscriptionProvider,
        providers.TextToSpeechProvider,
        providers.ImageGenerationProvider,
        providers.VideoGenerationProvider,
        providers.SearchProvider,
        providers.PublishingProvider,
        providers.AnalyticsProvider,
        providers.AffiliateProvider,
        providers.StorageProvider,
    ],
)
def test_every_protocol_rejects_incomplete_implementation(protocol: Any) -> None:
    """A partial adapter never satisfies the contract it claims to implement."""
    assert not isinstance(_Empty(), protocol)


def test_protocols_do_not_embed_concrete_vendor_identifiers() -> None:
    """The boundary stays provider-neutral: no vendor SDK or model identifier."""
    source = providers.__file__
    assert source is not None
    text = Path(source).read_text(encoding="utf-8")
    for forbidden in ("import boto3", "import openai", "import anthropic", "import google"):
        assert forbidden not in text
