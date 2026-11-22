"""Data access for record_key."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.events.models import Event
from app.modules.records.models import RecordKey


class RecordKeyRepository(Protocol):
    def get(self, event_id: UUID) -> RecordKey | None: ...

    def add(self, key: RecordKey) -> None: ...

    def live_past_retention(self, now: datetime) -> list[UUID]:
        """Events with a key not yet destroyed whose retain_until is at or before `now`, earliest
        first. The erasure job applies its own, later cutoff to each (records.service)."""
        ...


class SqlRecordKeyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, event_id: UUID) -> RecordKey | None:
        return self._session.get(RecordKey, event_id)

    def add(self, key: RecordKey) -> None:
        self._session.add(key)
        self._session.flush()

    def live_past_retention(self, now: datetime) -> list[UUID]:
        stmt = (
            select(RecordKey.event_id)
            .join(Event, Event.id == RecordKey.event_id)
            .where(RecordKey.destroyed_at.is_(None), Event.retain_until <= now)
            .order_by(Event.retain_until, Event.id)
        )
        return list(self._session.scalars(stmt))
