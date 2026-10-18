"""Response bodies for /v1/ledger/* and /v1/events/{event_id}/audit (Section 5.1)."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.types import UtcDatetime
from app.modules.ledger.chain import VerifyResult, format_timestamp
from app.modules.ledger.models import LedgerEntry
from app.modules.ledger.service import LedgerPage

HASH_ALGORITHM = (
    "entry_hash = sha256(canonical JSON of seq, entry_type, event_id, actor_id, at, payload, "
    "prev_hash; sorted keys, no whitespace, ASCII). First prev_hash is 64 zeros. "
    "See scripts/verify_ledger_export.py."
)


class LedgerEntryOut(BaseModel):
    seq: int
    entry_type: str
    event_id: UUID | None
    actor_id: UUID | None
    # Exactly the string that was hashed (UTC, microseconds, "+00:00").
    at: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str

    @classmethod
    def from_entry(cls, e: LedgerEntry) -> "LedgerEntryOut":
        return cls(
            seq=e.seq,
            entry_type=e.entry_type,
            event_id=e.event_id,
            actor_id=e.actor_id,
            at=format_timestamp(e.at),
            payload=e.payload,
            prev_hash=e.prev_hash,
            entry_hash=e.entry_hash,
        )


class LedgerExportOut(BaseModel):
    hash_algorithm: str = HASH_ALGORITHM
    entries: list[LedgerEntryOut]
    next_after_seq: int | None = Field(
        description="Pass as after_seq for the next page; null once the end is reached"
    )

    @classmethod
    def from_page(cls, page: LedgerPage) -> "LedgerExportOut":
        return cls(
            entries=[LedgerEntryOut.from_entry(e) for e in page.entries],
            next_after_seq=page.next_after_seq,
        )


class LedgerVerifyOut(BaseModel):
    valid: bool
    entries_checked: int
    first_mismatch: int | None = Field(description="seq of the first entry that fails, or null")
    head_seq: int
    head_hash: str

    @classmethod
    def from_result(cls, r: VerifyResult) -> "LedgerVerifyOut":
        return cls(
            valid=r.valid,
            entries_checked=r.entries_checked,
            first_mismatch=r.first_mismatch,
            head_seq=r.head_seq,
            head_hash=r.head_hash,
        )


class AuditEntryOut(BaseModel):
    actor_id: UUID | None = Field(description="null for actions taken by the background worker")
    action: str
    timestamp: UtcDatetime
    seq: int

    @classmethod
    def from_entry(cls, e: LedgerEntry) -> "AuditEntryOut":
        return cls(actor_id=e.actor_id, action=e.entry_type, timestamp=e.at, seq=e.seq)
