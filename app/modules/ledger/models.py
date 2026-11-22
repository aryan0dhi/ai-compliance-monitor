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
    """Model 2 ledger_entry.entry_type values."""

    FLAGGED = "FLAGGED"  # threshold breach event created (REQ-03)
    VIEW = "VIEW"  # event record read through the API (REQ-14)
    GAP_OPENED = "GAP_OPENED"  # monitoring-gap event created (REQ-02)
    GAP_CLOSED = "GAP_CLOSED"  # data resumed for an open gap (REQ-02, Ex-3)
    NOTIFIED = "NOTIFIED"  # a notification reached a reviewer webhook (REQ-04)
    # A reviewer's disposition or revision, with the harm fields it confirmed (REQ-08a/b, REQ-09).
    # One entry per call (B-14), so field confirmations have no entry type of their own.
    DISPOSITION = "DISPOSITION"
    # The Article 73 report of the current determination was assembled and stored (REQ-12, B-12).
    # Later reads of the same report are VIEW entries.
    REPORT_ASSEMBLED = "REPORT_ASSEMBLED"
    # A reviewer recorded that the report went to the authority (REQ-12, markReportSubmitted).
    REPORT_SUBMITTED = "REPORT_SUBMITTED"
    # A disposition or report call on an existing event was refused (403/409); nothing changed.
    # Written in its own transaction after the refusal (B-14, Model 5 Ex-4; D-26).
    REFUSED = "REFUSED"
    # The event's record key was destroyed at the end of its retention period, so its personal
    # data is unreadable from now on (REQ-16, AC-18; D-28). Written by the worker (actor null).
    PERSONAL_DATA_ERASED = "PERSONAL_DATA_ERASED"
    # An event's personal data was erased, on request (POST /v1/events/{id}/erasure) or at the
    # end of its retention period; payload `trigger` MANUAL or RETENTION (contract delta A4,
    # \u03946; D-29). Replaces PERSONAL_DATA_ERASED, which stays valid for entries written
    # before migration 0014.
    RECORD_ERASED = "RECORD_ERASED"
    # An admin registered a webhook (POST /v1/webhooks). The payload has `url_sha256`, never the
    # URL (contract delta \u03948; D-29). Not about an event, so event_id is null.
    WEBHOOK_REGISTERED = "WEBHOOK_REGISTERED"


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


class VerificationTrigger(StrEnum):
    """What started a ledger verification run (contract delta \u03945; D-29)."""

    SCHEDULED = "SCHEDULED"  # the worker's daily check (#71)
    MANUAL = "MANUAL"  # POST /v1/ledger/verifications by a reviewer


class HeadCheck(StrEnum):
    """How the chain's end compared with the head of the last passing run (D-14, D-27)."""

    MATCH = "MATCH"  # that entry still exists with the recorded hash
    MISMATCH = "MISMATCH"  # entries were removed from, or replaced at, the end
    NO_ANCHOR = "NO_ANCHOR"  # no earlier passing run to compare with


class LedgerCheck(Base):
    """One scheduled verification of the whole chain by the worker (REQ-06, Model 5 Ex-5, #71).

    Besides re-walking the chain, each check compares it with the head of the last check that
    passed: entry `head_seq` must still exist with hash `head_hash`. That detects entries removed
    from the end, which the chain alone can't show (D-14). Rows are never updated.
    """

    __tablename__ = "ledger_check"
    __table_args__ = (
        CheckConstraint("entries_checked >= 0", name="entries_checked_non_negative"),
        CheckConstraint("head_seq >= 0", name="head_seq_non_negative"),
        CheckConstraint("head_hash ~ '^[0-9a-f]{64}$'", name="head_hash_sha256"),
        # A check fails exactly when it names the problem it found.
        CheckConstraint("valid = (problem IS NULL)", name="valid_iff_no_problem"),
        CheckConstraint("NOT (valid AND alerted)", name="only_failures_alert"),
        check_in("trigger", VerificationTrigger, name="trigger"),
        check_in("head_check", HeadCheck, name="head_check"),
        Index("ix_ledger_check_checked_at", "checked_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    # core.clock time of the check.
    checked_at: Mapped[datetime]
    valid: Mapped[bool]
    entries_checked: Mapped[int] = mapped_column(BigInteger)
    # Last entry that verified (0 and 64 zeros for an empty ledger).
    head_seq: Mapped[int] = mapped_column(BigInteger)
    head_hash: Mapped[str]
    # seq of the first entry that doesn't verify, if the chain itself is broken.
    first_mismatch: Mapped[int | None] = mapped_column(BigInteger)
    problem: Mapped[str | None]
    # A ledger_alert was queued: the first failure after a passing check (one per incident).
    alerted: Mapped[bool]
    # Contract delta \u03945: SCHEDULED or MANUAL. Rows before migration 0016 are SCHEDULED.
    trigger: Mapped[str] = mapped_column(server_default=VerificationTrigger.SCHEDULED.value)
    # Null for rows written before migration 0016.
    head_check: Mapped[str | None]
