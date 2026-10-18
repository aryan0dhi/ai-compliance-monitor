"""Data access for monitored_system."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.systems.models import MonitoredSystem


class SystemRepository(Protocol):
    def get(self, system_id: UUID) -> MonitoredSystem | None: ...

    def get_for_update(self, system_id: UUID) -> MonitoredSystem | None:
        """The system, locked until the end of the transaction (waits for concurrent ingestion)."""
        ...

    def list_reporting(self) -> list[MonitoredSystem]:
        """Systems that have sent at least one sample (last_seen_at set)."""
        ...

    def add(self, system: MonitoredSystem) -> None: ...

    def touch_last_seen(self, system: MonitoredSystem, at: datetime) -> None:
        """Record that the system sent data at `at` (read by the REQ-02 gap sweeper)."""
        ...


class SqlSystemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, system_id: UUID) -> MonitoredSystem | None:
        return self._session.get(MonitoredSystem, system_id)

    def get_for_update(self, system_id: UUID) -> MonitoredSystem | None:
        return self._session.get(MonitoredSystem, system_id, with_for_update=True)

    def list_reporting(self) -> list[MonitoredSystem]:
        stmt = (
            select(MonitoredSystem)
            .where(MonitoredSystem.last_seen_at.is_not(None))
            .order_by(MonitoredSystem.id)
        )
        return list(self._session.scalars(stmt))

    def add(self, system: MonitoredSystem) -> None:
        self._session.add(system)
        self._session.flush()

    def touch_last_seen(self, system: MonitoredSystem, at: datetime) -> None:
        system.last_seen_at = at
