"""GET /v1/events and GET /v1/events/{event_id} response bodies (Phase 2, Section 5.2 Event)."""

from typing import Any

from pydantic import BaseModel

from app.core.types import IdOut, JsonDecimal, UtcDatetime
from app.modules.events.models import Event, EventState, EventType
from app.modules.events.service import EventPage, EventRecord
from app.modules.review.models import HarmField, HarmFieldStatus


class EventSummary(BaseModel):
    event_id: IdOut
    system_id: IdOut
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


class HarmFieldOut(BaseModel):
    """One Art. 3(49) field (pod interface HarmField, B-07)."""

    status: HarmFieldStatus
    prefill_evidence: str | None
    confirmed_by: IdOut | None
    confirmed_at: UtcDatetime | None

    @classmethod
    def from_field(cls, f: HarmField) -> "HarmFieldOut":
        return cls(
            status=HarmFieldStatus(f.status),
            prefill_evidence=f.prefill_evidence,
            confirmed_by=f.confirmed_by,
            confirmed_at=f.confirmed_at,
        )


class EventDetail(BaseModel):
    """Complete Event object.

    harm_fields (REQ-07) holds all four Art. 3(49) fields for breach events, and is null for
    monitoring gaps (INV-12). deadline_utc and revision_history come with disposition (REQ-09,
    REQ-10) and are null / empty until then.
    """

    event_id: IdOut
    system_id: IdOut
    type: EventType
    state: EventState
    detected_at: UtcDatetime
    deadline_utc: UtcDatetime | None = None
    harm_fields: dict[str, HarmFieldOut] | None = None
    revision_history: list[dict[str, Any]] = []
    # REQ-03 breach record (null for monitoring_gap events).
    metric_name: str | None
    metric_timestamp: UtcDatetime | None
    observed_value: JsonDecimal | None
    threshold_value: JsonDecimal | None
    threshold_version_id: IdOut | None
    # REQ-05 storage.
    retain_until: UtcDatetime
    evidence_bundle_hash: str | None
    # REQ-02 monitoring gap (null for threshold_breach events): last data before the gap, first
    # data after it, and the gap's length once closed (Ex-3).
    gap_started_at: UtcDatetime | None = None
    gap_ended_at: UtcDatetime | None = None
    gap_duration_seconds: int | None = None

    @classmethod
    def from_record(cls, record: EventRecord) -> "EventDetail":
        detail = cls.from_event(record.event)
        if record.harm_fields:
            detail.harm_fields = {f.name: HarmFieldOut.from_field(f) for f in record.harm_fields}
        return detail

    @classmethod
    def from_event(cls, e: Event) -> "EventDetail":
        duration = None
        if e.gap_started_at is not None and e.gap_ended_at is not None:
            duration = int((e.gap_ended_at - e.gap_started_at).total_seconds())
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
            gap_started_at=e.gap_started_at,
            gap_ended_at=e.gap_ended_at,
            gap_duration_seconds=duration,
        )
