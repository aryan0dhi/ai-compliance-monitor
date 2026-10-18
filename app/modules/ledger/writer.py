"""Production ledger writer: appends hash-chained entries in the caller's transaction."""

from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.modules.ledger.chain import GENESIS_HASH, compute_entry_hash, require_json_safe
from app.modules.ledger.models import LedgerEntry, LedgerEntryType

# pg_advisory_xact_lock key that serializes appends ("LEDG"). Held until the caller's transaction
# ends, so the next writer always sees the previous entry committed and the chain stays linear.
LEDGER_LOCK_KEY = 0x4C454447


class SqlLedgerWriter:
    def __init__(self, clock_provider: Callable[[], Clock] = get_clock) -> None:
        # Resolved per append so the test environment's simulated clock is picked up.
        self._clock_provider = clock_provider

    def append(
        self,
        session: Session | None,
        *,
        entry_type: str,
        actor_id: UUID | None,
        payload: dict[str, Any],
        event_id: UUID | None = None,
    ) -> None:
        if session is None:
            raise RuntimeError("the ledger must be given the caller's database session")
        entry_type = LedgerEntryType(entry_type).value
        require_json_safe(payload)

        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": LEDGER_LOCK_KEY})
        head = session.execute(
            select(LedgerEntry.seq, LedgerEntry.entry_hash)
            .order_by(LedgerEntry.seq.desc())
            .limit(1)
        ).first()
        seq, prev_hash = (head.seq + 1, head.entry_hash) if head else (1, GENESIS_HASH)
        at = self._clock_provider().now()
        entry = LedgerEntry(
            seq=seq,
            entry_type=entry_type,
            event_id=event_id,
            actor_id=actor_id,
            at=at,
            payload=payload,
            prev_hash=prev_hash,
            entry_hash=compute_entry_hash(
                seq=seq,
                entry_type=entry_type,
                event_id=event_id,
                actor_id=actor_id,
                at=at,
                payload=payload,
                prev_hash=prev_hash,
            ),
        )
        session.add(entry)
        session.flush()
