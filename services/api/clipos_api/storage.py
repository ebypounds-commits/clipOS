"""S3-compatible object storage with signed URLs (M2, CLIP-011).

Backed by any S3-compatible endpoint (MinIO locally per ``02_SYSTEM_ARCHITECTURE.md``;
S3/R2 in production). Credentials are supplied as environment *references* only
(``STORAGE_ACCESS_KEY`` / ``STORAGE_SECRET_KEY``), per ``07_SECURITY_GOVERNANCE_RIGHTS.md``
— they are never written to code, logs, or audit events, and ``StorageConfig``
redaction keeps them out of ``repr``/``str`` output.

Exception mapping: botocore ``ClientError`` never escapes this module's API
surface. ``404`` / ``NoSuchKey`` responses are mapped to ``ObjectNotFound``;
all other ``ClientError`` instances are re-raised as ``StorageClientError`` so
callers see a single domain-level exception type. Future work (out of scope
here): multipart/resumable uploads via Temporal and CDN delivery.
"""

from __future__ import annotations

import os
from types import TracebackType
from typing import Any, BinaryIO, Protocol, runtime_checkable

import boto3  # type: ignore[import-untyped]
from botocore.client import BaseClient  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from pydantic import BaseModel, Field, SecretStr

from clipos_api import audit

_DEFAULT_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "video/mp4",
        "video/webm",
        "application/pdf",
    }
)


class StorageConfigurationError(RuntimeError):
    """Raised when required STORAGE_* environment variables are missing."""


class ObjectNotFound(KeyError):
    """Requested key does not exist in the bucket (maps 404/NoSuchKey)."""

    def __init__(self, bucket: str, key: str) -> None:
        super().__init__(key)
        self.bucket = bucket
        self.key = key


class PolicyViolation(ValueError):
    """Upload rejected by the configured content-type/size policy."""


class StorageClientError(RuntimeError):
    """Wrapped non-404 botocore ClientError (credentials are never embedded)."""


class StorageConfig(BaseModel):
    """Connection settings; the secret is a `SecretStr` and never repr'd."""

    endpoint_url: str
    access_key_id: str
    secret_access_key: SecretStr
    bucket: str
    region_name: str = "us-east-1"
    presign_ttl_seconds: int = Field(default=3600, ge=1)

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> StorageConfig:
        env = os.environ if environ is None else environ
        missing = [
            name
            for name in (
                "STORAGE_ENDPOINT",
                "STORAGE_ACCESS_KEY",
                "STORAGE_SECRET_KEY",
                "STORAGE_BUCKET",
            )
            if not env.get(name)
        ]
        if missing:
            raise StorageConfigurationError(f"missing required env vars: {', '.join(missing)}")
        try:
            ttl = int(env.get("STORAGE_PRESIGN_TTL_SECONDS", "3600"))
        except ValueError as error:
            raise StorageConfigurationError(
                "STORAGE_PRESIGN_TTL_SECONDS must be an integer"
            ) from error
        return cls(
            endpoint_url=env["STORAGE_ENDPOINT"],
            access_key_id=env["STORAGE_ACCESS_KEY"],
            secret_access_key=SecretStr(env["STORAGE_SECRET_KEY"]),
            bucket=env["STORAGE_BUCKET"],
            region_name=env.get("STORAGE_REGION", "us-east-1"),
            presign_ttl_seconds=ttl,
        )


class PutPolicy(BaseModel):
    """Foundation-level upload policy: content-type allowlist + max size."""

    allowed_content_types: frozenset[str] = _DEFAULT_ALLOWED_CONTENT_TYPES
    max_bytes: int = Field(default=100 * 1024 * 1024, ge=1)

    def validate(self, key: str, content_type: str, size: int) -> None:  # type: ignore[override]
        if not key:
            raise PolicyViolation("object key is required")
        if content_type not in self.allowed_content_types:
            raise PolicyViolation(f"content type not allowed: {content_type}")
        if size < 0:
            raise PolicyViolation("object size must be non-negative")
        if size > self.max_bytes:
            raise PolicyViolation(f"object exceeds max size {self.max_bytes} bytes")


_DEFAULT_POLICY = PutPolicy()


@runtime_checkable
class ObjectStorage(Protocol):
    """Storage contract; concrete backends implement these operations."""

    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str,
        policy: PutPolicy | None = None,
    ) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def presign_get(self, key: str, ttl_seconds: int | None = None) -> str: ...

    def presign_put(
        self, key: str, content_type: str | None = None, ttl_seconds: int | None = None
    ) -> str: ...

    def ensure_bucket(self) -> None: ...


class S3ObjectStorage:
    """`ObjectStorage` backed by boto3 against any S3-compatible endpoint."""

    def __init__(self, config: StorageConfig, client: BaseClient | None = None) -> None:
        self._config = config
        self._client = client if client is not None else self._build_client(config)

    @staticmethod
    def _build_client(config: StorageConfig) -> BaseClient:
        return boto3.client(
            "s3",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key.get_secret_value(),
            region_name=config.region_name,
        )

    def put(
        self,
        key: str,
        data: bytes | BinaryIO,
        content_type: str,
        policy: PutPolicy | None = None,
    ) -> None:
        if policy is None:
            policy = _DEFAULT_POLICY
        if isinstance(data, bytes):
            size = len(data)
        else:
            if not (hasattr(data, "seek") and hasattr(data, "tell")):
                raise PolicyViolation("size cannot be determined for non-seekable fileobj")
            size = data.tell() - data.seek(0)
        policy.validate(key, content_type, size)
        try:
            self._client.put_object(
                Bucket=self._config.bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )
        except ClientError as error:
            raise self._wrap(error, key) from error
        audit.record(
            "storage.object_put",
            actor="storage-service",
            subject=key,
            details={
                "bucket": self._config.bucket,
                "key": key,
                "size": len(data) if isinstance(data, bytes) else None,
                "content_type": content_type,
            },
        )

    def get(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._config.bucket, Key=key)
        except ClientError as error:
            raise self._wrap(error, key) from error
        body = response["Body"]
        data: bytes = body.read()
        body.close()
        return data

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._config.bucket, Key=key)
        except ClientError as error:
            raise self._wrap(error, key) from error
        audit.record(
            "storage.object_deleted",
            actor="storage-service",
            subject=key,
            details={"bucket": self._config.bucket, "key": key},
        )

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._config.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise self._wrap(error, key) from error
        return True

    def presign_get(self, key: str, ttl_seconds: int | None = None) -> str:
        return self._presign("get_object", key, ttl_seconds)

    def presign_put(
        self, key: str, content_type: str | None = None, ttl_seconds: int | None = None
    ) -> str:
        extra: dict[str, Any] = {}
        if content_type is not None:
            extra["ContentType"] = content_type
        return self._presign("put_object", key, ttl_seconds, extra)

    def ensure_bucket(self) -> None:
        try:
            self._client.create_bucket(Bucket=self._config.bucket)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code == "BucketAlreadyOwnedByYou":
                return
            raise self._wrap(error) from error

    def _presign(
        self, method: str, key: str, ttl_seconds: int | None, extra: dict[str, Any] | None = None
    ) -> str:
        params: dict[str, Any] = {"Bucket": self._config.bucket, "Key": key, **(extra or {})}
        return str(
            self._client.generate_presigned_url(
                ClientMethod=method,
                Params=params,
                ExpiresIn=ttl_seconds or self._config.presign_ttl_seconds,
            )
        )

    @staticmethod
    def _wrap(error: ClientError, key: str | None = None) -> StorageClientError | ObjectNotFound:
        status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        code = error.response.get("Error", {}).get("Code", "")
        if status == 404 or code in ("NoSuchKey", "NotFound"):
            if key is not None:
                return ObjectNotFound("unknown", key)
            return StorageClientError(f"object not found (status={status}, code={code})")
        # Never propagate botocore exception objects or credential material.
        return StorageClientError(f"S3 client error (status={status}, code={code})")


class StorageSession:
    """Context-manager adapter so `with S3ObjectStorage(...) as storage:` works."""

    def __init__(self, storage: S3ObjectStorage) -> None:
        self._storage = storage

    def __enter__(self) -> S3ObjectStorage:
        return self._storage

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None
