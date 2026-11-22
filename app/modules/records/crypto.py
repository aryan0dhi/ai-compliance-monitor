"""Encryption primitives for record keys (REQ-16). Pure functions, no database.

- Each event has a random 256-bit data key. It is stored only wrapped (AES-256-GCM) by the master
  key from RECORD_KEY_MASTER_KEY, never in clear.
- A personal-data value is sealed with the event's data key (AES-256-GCM) into a text token
  `pd1:<base64url(nonce || ciphertext)>` (contract delta A3). Tokens written before it use
  `enc:v1:` and still open. The token is what the database column and the ledger
  payload hold, so ledger hashes are over ciphertext and survive the key's destruction.
- The associated data binds a token to its event and purpose, so a token copied into another
  event or field fails to open instead of showing the wrong text.
"""

import base64
import binascii
import hashlib
import os
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SEALED_PREFIX = "pd1:"
# Tokens sealed before contract delta A3; read, never written.
LEGACY_SEALED_PREFIX = "enc:v1:"
KEY_BYTES = 32
_NONCE_BYTES = 12

# Used only outside production when RECORD_KEY_MASTER_KEY is unset (development, CI, the test
# deployment before its key is configured). It is public, so it protects nothing.
DEVELOPMENT_MASTER_KEY = hashlib.sha256(b"ai-compliance-monitor development record key").digest()


class RecordKeyError(Exception):
    """A key or token is malformed, or doesn't belong to this event and purpose."""


def parse_master_key(value: str) -> bytes:
    """A base64 (standard or url-safe) encoding of exactly 32 bytes."""
    try:
        key = base64.b64decode(value.strip().replace("-", "+").replace("_", "/"), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("RECORD_KEY_MASTER_KEY must be base64") from exc
    if len(key) != KEY_BYTES:
        raise ValueError(f"RECORD_KEY_MASTER_KEY must decode to {KEY_BYTES} bytes")
    return key


def new_data_key() -> bytes:
    return AESGCM.generate_key(bit_length=KEY_BYTES * 8)


def _wrap_aad(event_id: UUID) -> bytes:
    return b"record_key:" + event_id.bytes


def wrap_key(master: bytes, event_id: UUID, data_key: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(master).encrypt(nonce, data_key, _wrap_aad(event_id))


def unwrap_key(master: bytes, event_id: UUID, wrapped: bytes) -> bytes:
    try:
        key = AESGCM(master).decrypt(
            wrapped[:_NONCE_BYTES], wrapped[_NONCE_BYTES:], _wrap_aad(event_id)
        )
    except InvalidTag as exc:
        raise RecordKeyError("the record key can't be unwrapped with this master key") from exc
    if len(key) != KEY_BYTES:  # pragma: no cover - only keys of this size are ever wrapped
        raise RecordKeyError("unwrapped record key has the wrong size")
    return key


def _field_aad(event_id: UUID, purpose: str) -> bytes:
    return f"{event_id}|{purpose}".encode()


def is_token(value: str | None) -> bool:
    return value is not None and value.startswith((SEALED_PREFIX, LEGACY_SEALED_PREFIX))


def seal(data_key: bytes, event_id: UUID, purpose: str, plaintext: str) -> str:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = nonce + AESGCM(data_key).encrypt(
        nonce, plaintext.encode("utf-8"), _field_aad(event_id, purpose)
    )
    return SEALED_PREFIX + base64.urlsafe_b64encode(sealed).decode("ascii")


def open_token(data_key: bytes, event_id: UUID, purpose: str, token: str) -> str:
    if not is_token(token):
        raise RecordKeyError("not a sealed value")
    try:
        prefix = SEALED_PREFIX if token.startswith(SEALED_PREFIX) else LEGACY_SEALED_PREFIX
        raw = base64.urlsafe_b64decode(token[len(prefix) :].encode("ascii"))
        plaintext = AESGCM(data_key).decrypt(
            raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _field_aad(event_id, purpose)
        )
    except (InvalidTag, binascii.Error, ValueError) as exc:
        raise RecordKeyError(f"sealed {purpose} doesn't open for event {event_id}") from exc
    return plaintext.decode("utf-8")
