"""GET /v1/events and GET /v1/events/{event_id} response bodies (Phase 2, Section 5.2 Event)."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from app.core.types import JsonDecimal, UtcDatetime
from app.modules.events.models import Event, EventState, EventType
from app.modules.events.service import EventPage


class EventSummary(BaseModel):
    event_id: UUID
    system_id: UUID
    type: EventType
    state: EventState
    detected_at: UtcDatetime
    metric_name: str | None

    @classmethod
    def from_event(cls, e: Event) -> "EventSummary":
        return cls(
            event_id=e.id,
            system_id=e.system_id,
            type=EventType(e.type),
            state=EventState(e.state),
            detected_at=e.detected_at,
            metric_name=e.metric_name,
        )


class EventPageOut(BaseModel):
    """Pagination envelope. Section 5 only says "paginated array"; shape is interim (D-06)."""

    items: list[EventSummary]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: EventPage) -> "EventPageOut":
        return cls(
            items=[EventSummary.from_event(e) for e in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )


class EventDetail(BaseModel):
    """Complete Event object.

    deadline_utc, harm_fields and revision_history are part of the Section 5.2 Event but are
    produced by Phase 4 review/disposition (REQ-07, REQ-09, REQ-10). Until then they are always
    null / empty.
    """

    event_id: UUID
    system_id: UUID
    type: EventType
    state: EventState
    detected_at: UtcDatetime
    deadline_utc: UtcDatetime | None = None
    harm_fields: dict[str, Any] | None = None
    revision_history: list[dict[str, Any]] = []
    # REQ-03 breach record (null for monitoring_gap events).
    metric_name: str | None
    metric_timestamp: UtcDatetime | None
    observed_value: JsonDecimal | None
    threshold_value: JsonDecimal | None
    threshold_version_id: UUID | None
    # REQ-05 storage.
    retain_until: UtcDatetime
    evidence_bundle_hash: str | None

    @classmethod
    def from_event(cls, e: Event) -> "EventDetail":
        return cls(
            event_id=e.id,
            system_id=e.system_id,
            type=EventType(e.type),
            state=EventState(e.state),
            detected_at=e.detected_at,
            metric_name=e.metric_name,
            metric_timestamp=e.metric_timestamp,
            observed_value=e.observed_value,
            threshold_value=e.threshold_value,
            threshold_version_id=e.threshold_version_id,
            retain_until=e.retain_until,
            evidence_bundle_hash=e.evidence_bundle_hash,
        )
