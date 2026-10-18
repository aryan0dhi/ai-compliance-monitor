"""Read access to ledger_entry. Writes go through the LedgerWriter only."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ledger.models import LedgerEntry


class LedgerRepository(Protocol):
    def page(self, *, after_seq: int, limit: int) -> list[LedgerEntry]:
        """Entries with seq > after_seq, in seq order."""
        ...

    def for_event(self, event_id: UUID) -> list[LedgerEntry]:
        """Entries recorded for one event, in seq order."""
        ...


class SqlLedgerRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def page(self, *, after_seq: int, limit: int) -> list[LedgerEntry]:
        stmt = (
            select(LedgerEntry)
            .where(LedgerEntry.seq > after_seq)
            .order_by(LedgerEntry.seq)
            .limit(limit)
        )
        entries = list(self._session.scalars(stmt))
        # Detach each page so walking a long chain doesn't keep every entry in the session.
        for entry in entries:
            self._session.expunge(entry)
        return entries

    def for_event(self, event_id: UUID) -> list[LedgerEntry]:
        stmt = select(LedgerEntry).where(LedgerEntry.event_id == event_id).order_by(LedgerEntry.seq)
        return list(self._session.scalars(stmt))
