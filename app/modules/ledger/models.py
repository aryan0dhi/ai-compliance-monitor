"""ledger_entry: the hash-chained, append-only audit ledger (REQ-06, REQ-14; Model 2)."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in


class LedgerEntryType(StrEnum):
    """Model 2 ledger_entry.entry_type values. Phase 4 adds FIELD_CONFIRMED, DISPOSITION, ..."""

    FLAGGED = "FLAGGED"  # threshold breach event created (REQ-03)
    VIEW = "VIEW"  # event record read through the API (REQ-14)
    GAP_OPENED = "GAP_OPENED"  # monitoring-gap event created (REQ-02)
    GAP_CLOSED = "GAP_CLOSED"  # data resumed for an open gap (REQ-02, Ex-3)
    NOTIFIED = "NOTIFIED"  # a notification reached a reviewer webhook (REQ-04)


class LedgerEntry(Base):
    __tablename__ = "ledger_entry"
    __table_args__ = (
        check_in("entry_type", LedgerEntryType, name="entry_type"),
        CheckConstraint("seq > 0", name="seq_positive"),
        CheckConstraint("prev_hash ~ '^[0-9a-f]{64}$'", name="prev_hash_sha256"),
        CheckConstraint("entry_hash ~ '^[0-9a-f]{64}$'", name="entry_hash_sha256"),
        # A chain can't fork: no two entries may claim the same predecessor.
        UniqueConstraint("prev_hash", name="uq_ledger_entry_prev_hash"),
        # GET /v1/events/{id}/audit.
        Index("ix_ledger_entry_event_id_seq", "event_id", "seq"),
    )

    # Position in the chain, 1-based and gapless. Assigned by the writer, not a sequence, so a
    # rolled-back transaction never leaves a hole.
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    entry_type: Mapped[str]
    event_id: Mapped[UUID | None] = mapped_column(ForeignKey("event.id", ondelete="RESTRICT"))
    # app_user.id of the caller; NULL for the background worker. No FK: the ledger stays
    # self-contained and verifiable on its own (an export can be checked without the users).
    actor_id: Mapped[UUID | None]
    # core.clock time of the action.
    at: Mapped[datetime]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # entry_hash of entry seq - 1 (64 zeros for the first entry).
    prev_hash: Mapped[str]
    # sha256 over the canonical form of all the fields above (app.modules.ledger.chain).
    entry_hash: Mapped[str]
