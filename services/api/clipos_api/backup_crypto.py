"""Authenticated encryption for backup artifacts (CLIP-096).

The operations runbook's backup baseline requires a *daily encrypted full
backup*; CLIP-013 deliberately shipped the manifest primitives with no
encryption and left that gap open. This module closes it.

**Construction.** The repository has no third-party crypto dependency and
is deliberately dependency-light, so the envelope is built from stdlib
primitives only:

- a keystream derived from SHA-256 in counter mode (SHA-256 is a PRF;
  ``keystream_block_i = SHA256(key || nonce || counter_i)``), XORed with
  the plaintext;
- **encrypt-then-MAC**: HMAC-SHA256 over the full header and ciphertext;
- constant-time tag comparison via :func:`hmac.compare_digest`;
- independent encryption and MAC subkeys derived from the master key with
  labelled SHA-256 (so the same master key is never used directly for both
  purposes).

**Scope and honesty.** This is a correct encrypt-then-MAC AEAD built from a
hash and a MAC, not a substitute for a vetted AEAD implementation. The
envelope carries an explicit ``alg`` version field precisely so it can be
migrated: a future revision should switch ``alg`` to a libsodium/AES-GCM
construction and keep this version readable for old backups. Nothing here
claims side-channel resistance beyond constant-time tag comparison.

**Key material.** The master key is supplied by the caller as bytes, or
read from a named environment variable (``CLIPOS_BACKUP_KEY`` by default)
holding 64 hex characters. The key is never written to disk, never placed
in a manifest, and never logged: errors and manifests carry only the opaque
``key_ref`` (the variable name) and a truncated key fingerprint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass

MAGIC = b"CLIPOSBK"
VERSION = 1
ALG = "sha256-ctr-hmac-sha256"
HEADER_LEN = 4
DEFAULT_KEY_ENV = "CLIPOS_BACKUP_KEY"
KEY_BYTES = 32


class BackupCryptoError(RuntimeError):
    """Raised when key material is missing or an envelope fails to verify."""


@dataclass(frozen=True)
class EncryptedEnvelope:
    """One encrypted backup artifact."""

    header: dict
    ciphertext: bytes

    def to_bytes(self) -> bytes:
        """Serialize to ``MAGIC | version | header_len | header | ciphertext``."""
        header_bytes = json.dumps(self.header, sort_keys=True, separators=(",", ":")).encode()
        return (
            MAGIC
            + bytes([VERSION])
            + len(header_bytes).to_bytes(HEADER_LEN, "big")
            + header_bytes
            + self.ciphertext
        )

    @classmethod
    def from_bytes(cls, blob: bytes) -> EncryptedEnvelope:
        """Parse a serialized envelope, validating the framing."""
        if not blob.startswith(MAGIC):
            raise BackupCryptoError("not a ClipOS backup envelope (bad magic)")
        offset = len(MAGIC)
        if len(blob) < offset + 1 + HEADER_LEN:
            raise BackupCryptoError("truncated backup envelope")
        version = blob[offset]
        if version != VERSION:
            raise BackupCryptoError(f"unsupported envelope version: {version}")
        offset += 1
        header_len = int.from_bytes(blob[offset : offset + HEADER_LEN], "big")
        offset += HEADER_LEN
        if len(blob) < offset + header_len:
            raise BackupCryptoError("truncated backup envelope header")
        header_bytes = blob[offset : offset + header_len]
        offset += header_len
        try:
            header = json.loads(header_bytes)
        except json.JSONDecodeError as error:
            raise BackupCryptoError(f"malformed envelope header: {error}") from error
        if not isinstance(header, dict):
            raise BackupCryptoError("malformed envelope header: not an object")
        return cls(header=header, ciphertext=blob[offset:])


def load_key(key_env: str = DEFAULT_KEY_ENV) -> bytes:
    """Read the master key from ``key_env`` (64 hex characters)."""
    raw = os.environ.get(key_env)
    if not raw:
        raise BackupCryptoError(f"backup key is not configured ({key_env})")
    try:
        key = bytes.fromhex(raw.strip())
    except ValueError as error:
        raise BackupCryptoError(f"{key_env} is not valid hex") from error
    if len(key) != KEY_BYTES:
        raise BackupCryptoError(f"{key_env} must be {KEY_BYTES} bytes ({KEY_BYTES * 2} hex chars)")
    return key


def generate_key() -> str:
    """Generate a fresh 32-byte key as hex, for provisioning."""
    return secrets.token_hex(KEY_BYTES)


def _subkey(key: bytes, label: str) -> bytes:
    """Derive a purpose-bound subkey so one master key is never reused raw."""
    return hashlib.sha256(label.encode() + b"|" + key).digest()


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """SHA-256 counter-mode keystream (a PRF-based stream cipher)."""
    blocks: list[bytes] = []
    produced = 0
    counter = 0
    while produced < length:
        blocks.append(hashlib.sha256(key + nonce + counter.to_bytes(8, "big")).digest())
        produced += 32
        counter += 1
    return b"".join(blocks)[:length]


def key_fingerprint(key: bytes) -> str:
    """A short, non-secret label identifying which key encrypted an artifact."""
    return hashlib.sha256(b"fingerprint|" + key).hexdigest()[:16]


def encrypt_backup(
    plaintext: bytes,
    key: bytes,
    *,
    artifact_ref: str,
    key_env: str = DEFAULT_KEY_ENV,
) -> EncryptedEnvelope:
    """Encrypt one backup artifact with authenticated encryption."""
    if not plaintext:
        # An empty backup is almost certainly an upstream failure; refuse
        # rather than produce an unverifiable "successful" artifact.
        raise BackupCryptoError("refusing to encrypt an empty backup")
    nonce = secrets.token_bytes(16)
    enc_key = _subkey(key, "enc")
    mac_key = _subkey(key, "mac")
    ciphertext = bytes(
        a ^ b for a, b in zip(plaintext, _keystream(enc_key, nonce, len(plaintext)), strict=True)
    )
    header = {
        "alg": ALG,
        "version": VERSION,
        "nonce": nonce.hex(),
        "artifact_ref": artifact_ref,
        "key_ref": key_env,
        "key_fingerprint": key_fingerprint(key),
        "plaintext_bytes": len(plaintext),
    }
    tag = _mac(mac_key, header, ciphertext)
    header["tag"] = tag.hex()
    return EncryptedEnvelope(header=header, ciphertext=ciphertext)


def _mac(mac_key: bytes, header: dict, ciphertext: bytes) -> bytes:
    """Encrypt-then-MAC: the MAC covers the header (minus tag) and ciphertext."""
    mac_header = {k: v for k, v in header.items() if k != "tag"}
    canonical = json.dumps(mac_header, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(mac_key, canonical + b"|" + ciphertext, hashlib.sha256).digest()


def decrypt_backup(envelope: EncryptedEnvelope, key: bytes) -> bytes:
    """Verify the tag in constant time, then decrypt.

    Any tampering with the header or ciphertext raises
    :class:`BackupCryptoError`; no plaintext is released on failure.
    """
    header = dict(envelope.header)
    alg = header.get("alg")
    if alg != ALG:
        raise BackupCryptoError(f"unsupported envelope alg: {alg}")
    tag_hex = header.pop("tag", None)
    if not tag_hex:
        raise BackupCryptoError("envelope is missing its authentication tag")
    try:
        expected_tag = bytes.fromhex(tag_hex)
    except ValueError as error:
        raise BackupCryptoError("envelope tag is not valid hex") from error

    mac_key = _subkey(key, "mac")
    actual_tag = _mac(mac_key, header, envelope.ciphertext)
    if not hmac.compare_digest(expected_tag, actual_tag):
        raise BackupCryptoError("backup envelope failed authentication")

    try:
        nonce = bytes.fromhex(str(header["nonce"]))
    except (KeyError, ValueError) as error:
        raise BackupCryptoError("envelope nonce is missing or malformed") from error

    enc_key = _subkey(key, "enc")
    plaintext = bytes(
        a ^ b
        for a, b in zip(
            envelope.ciphertext,
            _keystream(enc_key, nonce, len(envelope.ciphertext)),
            strict=True,
        )
    )
    declared = header.get("plaintext_bytes")
    if declared is not None and int(declared) != len(plaintext):
        raise BackupCryptoError("envelope length does not match its header")
    return plaintext


def decrypt_blob(blob: bytes, key: bytes) -> bytes:
    """Convenience: parse then decrypt a serialized envelope."""
    return decrypt_backup(EncryptedEnvelope.from_bytes(blob), key)


__all__ = [
    "ALG",
    "DEFAULT_KEY_ENV",
    "MAGIC",
    "VERSION",
    "BackupCryptoError",
    "EncryptedEnvelope",
    "decrypt_backup",
    "decrypt_blob",
    "encrypt_backup",
    "generate_key",
    "key_fingerprint",
    "load_key",
]
