"""Data access for event."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import ColumnElement, Select, func, select
from sqlalchemy.orm import Session

from app.modules.events.models import OPEN_GAP_STATES, Event, EventState, EventType


@dataclass(frozen=True)
class EventFilters:
    """GET /v1/events query filters (Phase 2, Section 5.1). `since` filters on detected_at."""

    system_id: UUID | None = None
    state: EventState | None = None
    type: EventType | None = None
    since: datetime | None = None


class EventRepository(Protocol):
    def add(self, event: Event) -> None:
        """Insert the event and flush, so later writes in the transaction can reference it."""
        ...

    def get(self, event_id: UUID) -> Event | None: ...

    def find_breach_for_sample(
        self, system_id: UUID, metric_name: str, metric_timestamp: datetime
    ) -> Event | None: ...

    # Declared before list(): inside the class body, `list` names that method from then on.
    def get_for_update(self, event_id: UUID) -> Event | None:
        """The event, locked until the end of the transaction."""
        ...

    def open_gaps(self) -> list[Event]:
        """monitoring_gap events that haven't closed yet (REQ-02)."""
        ...

    def open_gap_for_system(self, system_id: UUID) -> Event | None: ...

    def list(self, filters: EventFilters, *, limit: int, offset: int) -> tuple[list[Event], int]:
        """Matching events, newest detected_at first, plus the total match count."""
        ...


def _open_gap_filter() -> ColumnElement[bool]:
    return (Event.type == EventType.MONITORING_GAP.value) & Event.state.in_(
        [s.value for s in OPEN_GAP_STATES]
    )


def _apply_filters(stmt: Select, filters: EventFilters) -> Select:
    if filters.system_id is not None:
        stmt = stmt.where(Event.system_id == filters.system_id)
    if filters.state is not None:
        stmt = stmt.where(Event.state == filters.state.value)
    if filters.type is not None:
        stmt = stmt.where(Event.type == filters.type.value)
    if filters.since is not None:
        stmt = stmt.where(Event.detected_at >= filters.since)
    return stmt


def build_list_query(filters: EventFilters, *, limit: int, offset: int) -> Select:
    # id breaks ties so pagination is stable when events share a detected_at.
    return (
        _apply_filters(select(Event), filters)
        .order_by(Event.detected_at.desc(), Event.id.desc())
        .limit(limit)
        .offset(offset)
    )


def build_count_query(filters: EventFilters) -> Select:
    return _apply_filters(select(func.count()).select_from(Event), filters)


class SqlEventRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: Event) -> None:
        self._session.add(event)
        self._session.flush()

    def get(self, event_id: UUID) -> Event | None:
        return self._session.get(Event, event_id)

    def find_breach_for_sample(
        self, system_id: UUID, metric_name: str, metric_timestamp: datetime
    ) -> Event | None:
        stmt = select(Event).where(
            Event.type == EventType.THRESHOLD_BREACH.value,
            Event.system_id == system_id,
            Event.metric_name == metric_name,
            Event.metric_timestamp == metric_timestamp,
        )
        return self._session.scalars(stmt).first()

    def get_for_update(self, event_id: UUID) -> Event | None:
        return self._session.get(Event, event_id, with_for_update=True)

    def open_gaps(self) -> list[Event]:
        stmt = select(Event).where(_open_gap_filter()).order_by(Event.detected_at, Event.id)
        return list(self._session.scalars(stmt))

    def open_gap_for_system(self, system_id: UUID) -> Event | None:
        stmt = select(Event).where(_open_gap_filter(), Event.system_id == system_id)
        return self._session.scalars(stmt).first()

    def list(self, filters: EventFilters, *, limit: int, offset: int) -> tuple[list[Event], int]:
        items = list(self._session.scalars(build_list_query(filters, limit=limit, offset=offset)))
        total = self._session.scalar(build_count_query(filters)) or 0
        return items, total
