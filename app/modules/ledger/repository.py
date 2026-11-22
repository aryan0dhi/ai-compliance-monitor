"""Read access to ledger_entry (writes go through the LedgerWriter only), and ledger_check."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.ledger.models import LedgerCheck, LedgerEntry


class LedgerRepository(Protocol):
    def page(self, *, after_seq: int, limit: int) -> list[LedgerEntry]:
        """Entries with seq > after_seq, in seq order."""
        ...

    def for_event(self, event_id: UUID) -> list[LedgerEntry]:
        """Entries recorded for one event, in seq order."""
        ...

    def head(self) -> LedgerEntry | None:
        """The entry with the highest seq, or None while the ledger is empty."""
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

    def head(self) -> LedgerEntry | None:
        return self._session.scalars(
            select(LedgerEntry).order_by(LedgerEntry.seq.desc()).limit(1)
        ).first()


class LedgerCheckRepository(Protocol):
    def add(self, check: LedgerCheck) -> None: ...

    def latest(self) -> LedgerCheck | None:
        """The most recent check, if any."""
        ...

    def latest_valid(self) -> LedgerCheck | None:
        """The most recent check that passed: the head later checks compare against."""
        ...

    def newest_first(self) -> list[LedgerCheck]:
        """Every check, newest first (GET /v1/ledger/verifications)."""
        ...


class SqlLedgerCheckRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, check: LedgerCheck) -> None:
        self._session.add(check)
        self._session.flush()

    def latest(self) -> LedgerCheck | None:
        stmt = select(LedgerCheck).order_by(LedgerCheck.checked_at.desc(), LedgerCheck.id).limit(1)
        return self._session.scalars(stmt).first()

    def latest_valid(self) -> LedgerCheck | None:
        stmt = (
            select(LedgerCheck)
            .where(LedgerCheck.valid)
            .order_by(LedgerCheck.checked_at.desc(), LedgerCheck.id)
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def newest_first(self) -> list[LedgerCheck]:
        stmt = select(LedgerCheck).order_by(LedgerCheck.checked_at.desc(), LedgerCheck.id)
        return list(self._session.scalars(stmt))
