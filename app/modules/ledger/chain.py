"""Hash-chain rules for the audit ledger (REQ-06, INV-7). Pure functions, no database.

Each entry's hash is sha256 over a canonical JSON document of its fields, including the previous
entry's hash, so changing, removing or reordering any entry breaks every link after it:

    entry_hash = sha256(json.dumps({
        "seq": int, "entry_type": str, "event_id": str | None, "actor_id": str | None,
        "at": str, "payload": object, "prev_hash": str,
    }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()

`at` is ISO-8601 in UTC with microseconds ("2026-09-27T20:01:00.000000+00:00") and the first
entry's prev_hash is 64 zeros. scripts/verify_ledger_export.py reimplements this with the
standard library only, so another team can verify an export independently.
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

GENESIS_HASH = "0" * 64


def format_timestamp(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("ledger timestamps must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def require_json_safe(value: Any, path: str = "payload") -> None:
    """Reject values whose JSON form isn't stable through PostgreSQL jsonb and back.

    Floats are refused (store decimals as strings, as event payloads already do): their text form
    can change on the round trip and would then no longer match the stored hash.
    """
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path}: keys must be strings")
            require_json_safe(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            require_json_safe(item, f"{path}[{i}]")
        return
    raise TypeError(f"{path}: {type(value).__name__} is not allowed in a ledger payload")


def compute_entry_hash(
    *,
    seq: int,
    entry_type: str,
    event_id: UUID | None,
    actor_id: UUID | None,
    at: datetime,
    payload: dict[str, Any],
    prev_hash: str,
) -> str:
    document = {
        "seq": seq,
        "entry_type": str(entry_type),
        "event_id": str(event_id) if event_id is not None else None,
        "actor_id": str(actor_id) if actor_id is not None else None,
        "at": format_timestamp(at),
        "payload": payload,
        "prev_hash": prev_hash,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ChainEntry(Protocol):
    @property
    def seq(self) -> int: ...
    @property
    def entry_type(self) -> str: ...
    @property
    def event_id(self) -> UUID | None: ...
    @property
    def actor_id(self) -> UUID | None: ...
    @property
    def at(self) -> datetime: ...
    @property
    def payload(self) -> dict[str, Any]: ...
    @property
    def prev_hash(self) -> str: ...
    @property
    def entry_hash(self) -> str: ...


@dataclass(frozen=True)
class VerifyResult:
    valid: bool
    entries_checked: int
    # seq of the first entry that doesn't verify (Section 5.1 first_mismatch), else None.
    first_mismatch: int | None
    # Last verified entry. Recording head_seq/head_hash and comparing them later is how removal of
    # entries at the END of the chain is detected; the chain alone can't show that (Phase 5 keeps
    # the head outside the database).
    head_seq: int
    head_hash: str


def verify_chain(entries: Iterable[ChainEntry]) -> VerifyResult:
    """Check entries in seq order: gapless from 1, each linked to its predecessor, hashes intact."""
    expected_seq, prev_hash, checked = 1, GENESIS_HASH, 0
    for entry in entries:
        checked += 1
        recomputed = compute_entry_hash(
            seq=entry.seq,
            entry_type=entry.entry_type,
            event_id=entry.event_id,
            actor_id=entry.actor_id,
            at=entry.at,
            payload=entry.payload,
            prev_hash=entry.prev_hash,
        )
        if (
            entry.seq != expected_seq
            or entry.prev_hash != prev_hash
            or entry.entry_hash != recomputed
        ):
            return VerifyResult(False, checked, entry.seq, expected_seq - 1, prev_hash)
        expected_seq, prev_hash = entry.seq + 1, entry.entry_hash
    return VerifyResult(True, checked, None, expected_seq - 1, prev_hash)
