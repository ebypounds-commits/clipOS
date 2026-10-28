"""Unit tests for the S3-compatible object storage foundation (CLIP-011)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from clipos_api import storage


class FakeS3Client:
    """Records calls and emulates presign URLs deterministically (no network)."""

    def __init__(self) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bucket_created = False

    def _record(self, method: str, params: dict[str, Any]) -> None:
        self.calls.append((method, params))

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("put_object", kwargs)
        self.objects[kwargs["Key"]] = kwargs
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_object", kwargs)
        if kwargs["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "GetObject",
            )
        return {"Body": io.BytesIO(self.objects[kwargs["Key"]]["Body"])}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("delete_object", kwargs)
        self.objects.pop(kwargs["Key"], None)
        return {}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        self._record("head_object", kwargs)
        if kwargs["Key"] not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NotFound"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )
        return {}

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        self._record("create_bucket", kwargs)
        self.bucket_created = True
        return {}

    def generate_presigned_url(
        self, *, ClientMethod: str, Params: dict[str, Any], ExpiresIn: int
    ) -> str:
        self._record(ClientMethod, {**Params, "ExpiresIn": ExpiresIn})
        return f"https://fake/{ClientMethod}/{Params['Bucket']}/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.fixture(autouse=True)
def _redirect_audit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(storage.audit, "AUDIT_PATH", tmp_path / "audit.log")


@pytest.fixture()
def fake_client() -> FakeS3Client:
    return FakeS3Client()


@pytest.fixture()
def config() -> storage.StorageConfig:
    return storage.StorageConfig(
        endpoint_url="http://localhost:9002",
        access_key_id="test-access",
        secret_access_key=storage.SecretStr("test-secret"),
        bucket="clipos-test",
    )


@pytest.fixture()
def store(config: storage.StorageConfig, fake_client: FakeS3Client) -> storage.S3ObjectStorage:
    return storage.S3ObjectStorage(config, client=fake_client)


def test_from_env_reads_config_and_redacts_secret() -> None:
    env = {
        "STORAGE_ENDPOINT": "http://localhost:9002",
        "STORAGE_ACCESS_KEY": "access",
        "STORAGE_SECRET_KEY": "supersecret",
        "STORAGE_BUCKET": "bucket",
        "STORAGE_REGION": "eu-central-1",
        "STORAGE_PRESIGN_TTL_SECONDS": "120",
    }
    config = storage.StorageConfig.from_env(env)
    assert config.endpoint_url == "http://localhost:9002"
    assert config.access_key_id == "access"
    assert config.region_name == "eu-central-1"
    assert config.presign_ttl_seconds == 120
    assert "supersecret" not in repr(config)
    assert "supersecret" not in str(config)


def test_from_env_requires_required_vars() -> None:
    with pytest.raises(storage.StorageConfigurationError):
        storage.StorageConfig.from_env({})


def test_policy_accepts_and_rejects() -> None:
    policy = storage.PutPolicy()
    policy.validate("a/b.png", "image/png", 10)
    with pytest.raises(storage.PolicyViolation):
        policy.validate("a.txt", "text/plain", 10)
    with pytest.raises(storage.PolicyViolation):
        policy.validate("a.png", "image/png", policy.max_bytes + 1)


def test_put_validates_then_uploads(
    store: storage.S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    store.put("media/a.png", b"data", "image/png")
    method, params = fake_client.calls[-1]
    assert method == "put_object"
    assert params["Bucket"] == "clipos-test"
    assert params["Key"] == "media/a.png"
    assert params["ContentType"] == "image/png"
    with pytest.raises(storage.PolicyViolation):
        store.put("bad.txt", b"x", "text/plain")
    with pytest.raises(storage.PolicyViolation):
        store.put("big.png", b"xxx", "image/png", policy=storage.PutPolicy(max_bytes=2))


def test_put_is_idempotent_overwrite(store: storage.S3ObjectStorage) -> None:
    store.put("media/a.png", b"one", "image/png")
    store.put("media/a.png", b"two", "image/png")
    assert store.get("media/a.png") == b"two"


def test_get_missing_raises_object_not_found(
    store: storage.S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    with pytest.raises(storage.ObjectNotFound):
        store.get("missing.png")
    assert fake_client.calls[-1][0] == "get_object"


def test_delete_is_idempotent(store: storage.S3ObjectStorage) -> None:
    store.put("media/a.png", b"data", "image/png")
    store.delete("media/a.png")
    store.delete("media/a.png")
    assert not store.exists("media/a.png")


def test_presign_urls(store: storage.S3ObjectStorage, fake_client: FakeS3Client) -> None:
    get_url = store.presign_get("media/a.png", ttl_seconds=120)
    put_url = store.presign_put("media/a.png", content_type="image/png", ttl_seconds=60)
    assert "X-Amz-Expires=120" in get_url
    assert "X-Amz-Expires=60" in put_url
    assert fake_client.calls[-2][0] == "get_object"
    assert fake_client.calls[-1][0] == "put_object"
    default_url = store.presign_get("media/a.png")
    assert "X-Amz-Expires=3600" in default_url


def test_ensure_bucket_idempotent(
    store: storage.S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    store.ensure_bucket()
    store.ensure_bucket()
    created = [call for call in fake_client.calls if call[0] == "create_bucket"]
    assert len(created) == 2
    assert fake_client.bucket_created


def test_put_delete_audit_events(
    store: storage.S3ObjectStorage,
    fake_client: FakeS3Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_path = tmp_path / "audit.log"
    store.put("media/a.png", b"payload", "image/png")
    store.delete("media/a.png")
    events = storage.audit.read_events(audit_path)
    actions = [event["action"] for event in events]
    assert actions == ["storage.object_put", "storage.object_deleted"]
    put_event = events[0]
    assert put_event["details"]["key"] == "media/a.png"
    assert put_event["details"]["size"] == 7
    assert put_event["details"]["content_type"] == "image/png"
    assert "payload" not in str(events)
    assert "test-secret" not in str(events)


# ----------------------------------------------------------------------
# Configuration errors
# ----------------------------------------------------------------------


def test_from_env_rejects_non_integer_ttl() -> None:
    env = {
        "STORAGE_ENDPOINT": "http://localhost:9002",
        "STORAGE_ACCESS_KEY": "access",
        "STORAGE_SECRET_KEY": "secret",
        "STORAGE_BUCKET": "bucket",
        "STORAGE_PRESIGN_TTL_SECONDS": "not-a-number",
    }
    with pytest.raises(storage.StorageConfigurationError, match="must be an integer"):
        storage.StorageConfig.from_env(env)


# ----------------------------------------------------------------------
# Policy rejections
# ----------------------------------------------------------------------


def test_policy_requires_object_key() -> None:
    with pytest.raises(storage.PolicyViolation, match="object key is required"):
        storage.PutPolicy().validate("", "image/png", 10)


def test_policy_rejects_negative_size() -> None:
    with pytest.raises(storage.PolicyViolation, match="non-negative"):
        storage.PutPolicy().validate("a.png", "image/png", -1)


def test_put_rejects_non_seekable_stream(
    store: storage.S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    class _NotSeekable:
        def read(self) -> bytes:
            return b""

    with pytest.raises(storage.PolicyViolation, match="non-seekable"):
        store.put("media/a.png", _NotSeekable(), "image/png")  # type: ignore[arg-type]
    assert fake_client.calls == []


def test_put_accepts_seekable_stream(
    store: storage.S3ObjectStorage, fake_client: FakeS3Client
) -> None:
    stream = io.BytesIO(b"0123456789")
    store.put("media/a.png", stream, "image/png")
    method, params = fake_client.calls[-1]
    assert method == "put_object"
    assert params["Body"] is stream


# ----------------------------------------------------------------------
# Client error mapping
# ----------------------------------------------------------------------


class _FailingS3Client(FakeS3Client):
    """Raises a non-404 ClientError for every mutating call."""

    def __init__(self, code: str = "AccessDenied", status: int = 403) -> None:
        super().__init__()
        self.code = code
        self.status = status

    def _error(self, operation: str) -> ClientError:
        return ClientError(
            {
                "Error": {"Code": self.code},
                "ResponseMetadata": {"HTTPStatusCode": self.status},
            },
            operation,
        )

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        raise self._error("PutObject")

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        raise self._error("DeleteObject")

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        raise self._error("HeadObject")

    def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
        raise self._error("CreateBucket")


@pytest.fixture()
def failing_store(config: storage.StorageConfig) -> storage.S3ObjectStorage:
    return storage.S3ObjectStorage(config, client=_FailingS3Client())


def test_put_client_error_is_wrapped(failing_store: storage.S3ObjectStorage) -> None:
    with pytest.raises(storage.StorageClientError, match="S3 client error"):
        failing_store.put("media/a.png", b"data", "image/png")


def test_delete_client_error_is_wrapped(failing_store: storage.S3ObjectStorage) -> None:
    with pytest.raises(storage.StorageClientError, match="S3 client error"):
        failing_store.delete("media/a.png")


def test_exists_client_error_is_wrapped(failing_store: storage.S3ObjectStorage) -> None:
    with pytest.raises(storage.StorageClientError, match="S3 client error"):
        failing_store.exists("media/a.png")


def test_ensure_bucket_client_error_is_wrapped(
    failing_store: storage.S3ObjectStorage,
) -> None:
    with pytest.raises(storage.StorageClientError, match="S3 client error"):
        failing_store.ensure_bucket()


def test_ensure_bucket_tolerates_already_owned_bucket(
    config: storage.StorageConfig,
) -> None:
    class _OwnedClient(FakeS3Client):
        def create_bucket(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {
                    "Error": {"Code": "BucketAlreadyOwnedByYou"},
                    "ResponseMetadata": {"HTTPStatusCode": 409},
                },
                "CreateBucket",
            )

    owned = storage.S3ObjectStorage(config, client=_OwnedClient())
    owned.ensure_bucket()


def test_wrap_without_key_reports_missing_object() -> None:
    error = ClientError(
        {
            "Error": {"Code": "NoSuchKey"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        },
        "HeadBucket",
    )
    wrapped = storage.S3ObjectStorage._wrap(error)
    assert isinstance(wrapped, storage.StorageClientError)
    assert "object not found" in str(wrapped)


def test_client_error_message_never_leaks_credentials() -> None:
    error = ClientError(
        {
            "Error": {"Code": "InvalidAccessKeyId"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        "PutObject",
    )
    wrapped = storage.S3ObjectStorage._wrap(error, "media/a.png")
    assert isinstance(wrapped, storage.StorageClientError)
    assert "test-secret" not in str(wrapped)
    assert "media/a.png" not in str(wrapped)


# ----------------------------------------------------------------------
# Session adapter
# ----------------------------------------------------------------------


def test_storage_session_yields_storage_and_closes_cleanly(
    store: storage.S3ObjectStorage,
) -> None:
    with storage.StorageSession(store) as session:
        assert session is store
    assert store is not None

