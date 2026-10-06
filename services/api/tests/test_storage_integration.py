"""MinIO-backed integration tests, activated only when STORAGE_ENDPOINT is set."""

from __future__ import annotations

import os
import uuid

import pytest

from clipos_api import storage

pytestmark = pytest.mark.skipif(
    not os.environ.get("STORAGE_ENDPOINT"),
    reason="STORAGE_ENDPOINT not set (in-sandbox runs have no MinIO; CI activates this)",
)


@pytest.fixture(scope="module")
def live_store() -> storage.S3ObjectStorage:
    return storage.S3ObjectStorage(storage.StorageConfig.from_env())


def test_minio_roundtrip(live_store: storage.S3ObjectStorage) -> None:
    live_store.ensure_bucket()
    key = f"integration/{uuid.uuid4()}.png"
    payload = b"clipos-storage-roundtrip"
    live_store.put(key, payload, "image/png")
    assert live_store.exists(key)
    assert live_store.get(key) == payload
    url = live_store.presign_get(key, ttl_seconds=60)
    assert "X-Amz-Expires=60" in url
    live_store.delete(key)
    assert not live_store.exists(key)
