"""Data access for monitored_system."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.systems.models import MonitoredSystem


class SystemRepository(Protocol):
    def get(self, system_id: UUID) -> MonitoredSystem | None: ...

    def touch_last_seen(self, system: MonitoredSystem, at: datetime) -> None:
        """Record that the system sent data at `at` (read by the REQ-02 gap sweeper)."""
        ...


class SqlSystemRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, system_id: UUID) -> MonitoredSystem | None:
        return self._session.get(MonitoredSystem, system_id)

    def touch_last_seen(self, system: MonitoredSystem, at: datetime) -> None:
        system.last_seen_at = at
