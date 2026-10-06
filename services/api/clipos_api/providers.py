"""Provider-neutral interfaces.

Concrete transport details stay in adapters. The placeholder words in comments are
generic compatibility descriptors only and are not configuration identifiers.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMProvider(Protocol):
    async def complete(
        self, *, alias: str, prompt: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class EmbeddingProvider(Protocol):
    async def embed(
        self, *, alias: str, inputs: Sequence[str], options: Mapping[str, Any]
    ) -> Sequence[Sequence[float]]:
        raise NotImplementedError


@runtime_checkable
class TranscriptionProvider(Protocol):
    async def transcribe(
        self, *, alias: str, media_ref: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class TextToSpeechProvider(Protocol):
    async def synthesize(
        self, *, alias: str, text: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class ImageGenerationProvider(Protocol):
    async def generate_image(
        self, *, alias: str, prompt: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class VideoGenerationProvider(Protocol):
    async def generate_video(
        self, *, alias: str, prompt: str, options: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class SearchProvider(Protocol):
    async def search(
        self, *, alias: str, query: str, options: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError


@runtime_checkable
class PublishingProvider(Protocol):
    async def publish(
        self, *, alias: str, payload: Mapping[str, Any], idempotency_key: str
    ) -> Mapping[str, Any]:
        raise NotImplementedError


@runtime_checkable
class AnalyticsProvider(Protocol):
    async def fetch_metrics(
        self, *, alias: str, query: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError


@runtime_checkable
class AffiliateProvider(Protocol):
    async def list_offers(
        self, *, alias: str, query: Mapping[str, Any]
    ) -> Sequence[Mapping[str, Any]]:
        raise NotImplementedError


@runtime_checkable
class StorageProvider(Protocol):
    async def put_object(
        self, *, alias: str, key: str, data: bytes, content_type: str
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    async def get_object(self, *, alias: str, key: str) -> bytes:
        raise NotImplementedError
