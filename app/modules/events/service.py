"""Event creation (REQ-03) and retrieval (GET /v1/events, GET /v1/events/{id}).

Review, disposition and deadline logic (Phase 4) belong in their own modules and reference
events by id.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.events.repository import EventFilters
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.metrics.models import MetricSample
from app.modules.notifications.interface import NotificationType, Notifier
from app.modules.thresholds.models import ThresholdVersion

# Roles allowed to read events. Monitored-system keys only submit metrics (Section 5.1).
EVENT_READER_ROLES = frozenset({Role.VIEWER, Role.REVIEWER, Role.ADMIN})


class EventAccessDenied(Exception):
    """The caller's role may not read events."""


class EventNotFound(Exception):
    pass


@dataclass(frozen=True)
class EventPage:
    items: list[Event]
    total: int
    limit: int
    offset: int


def event_payload(event: Event) -> dict[str, Any]:
    """JSON-safe snapshot of a breach event, for the ledger entry and the notification."""
    return {
        "event_id": str(event.id),
        "type": event.type,
        "state": event.state,
        "system_id": str(event.system_id),
        "metric_name": event.metric_name,
        "metric_timestamp": event.metric_timestamp.isoformat() if event.metric_timestamp else None,
        "observed_value": str(event.observed_value) if event.observed_value is not None else None,
        "threshold_version_id": str(event.threshold_version_id)
        if event.threshold_version_id
        else None,
        "threshold_value": str(event.threshold_value)
        if event.threshold_value is not None
        else None,
        "detected_at": event.detected_at.isoformat(),
        "retain_until": event.retain_until.isoformat(),
    }


def create_breach_event(
    uow: UnitOfWork,
    *,
    sample: MetricSample,
    threshold: ThresholdVersion,
    detected_at: datetime,
    actor_id: UUID | None,
    ledger: LedgerWriter,
    notifier: Notifier,
) -> Event:
    """Create a FLAGGED threshold_breach event inside the caller's unit of work.

    Writes the event, its FLAGGED ledger entry (REQ-06/14) and its flag notification (REQ-04)
    in the same transaction; if either integration fails, the whole transaction rolls back.
    """
    event = Event(
        id=uuid4(),
        type=EventType.THRESHOLD_BREACH.value,
        state=EventState.FLAGGED.value,
        system_id=sample.system_id,
        metric_name=sample.metric_name,
        metric_timestamp=sample.sampled_at,
        observed_value=sample.value,
        threshold_version_id=threshold.id,
        threshold_value=threshold.limit_value,
        detected_at=detected_at,
        retain_until=retain_until_for(detected_at),
    )
    uow.events.add(event)
    payload = event_payload(event)
    ledger.append(
        uow.session,
        entry_type=LedgerEntryType.FLAGGED,
        actor_id=actor_id,
        payload=payload,
        event_id=event.id,
    )
    notifier.enqueue(
        uow.session,
        notification_type=NotificationType.FLAG_NOTIFICATION,
        event_id=event.id,
        payload=payload,
    )
    return event


def _require_reader(principal: Principal) -> None:
    if principal.role not in EVENT_READER_ROLES:
        raise EventAccessDenied(f"role {principal.role} may not read events")


def list_events(
    filters: EventFilters,
    *,
    limit: int,
    offset: int,
    principal: Principal,
    uow: UnitOfWork,
) -> EventPage:
    """List events. Per Section 5.1 this has no state change, so no ledger entry is written."""
    _require_reader(principal)
    with uow:
        items, total = uow.events.list(filters, limit=limit, offset=offset)
    return EventPage(items=items, total=total, limit=limit, offset=offset)


def get_event(
    event_id: UUID,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
) -> Event:
    """Return one event and record the view in the ledger (REQ-14, Section 5.1).

    The VIEW entry is written in the same transaction as the read; if it can't be written, the
    request fails rather than returning an unaudited view.
    """
    _require_reader(principal)
    with uow:
        event = uow.events.get(event_id)
        if event is None:
            raise EventNotFound(str(event_id))
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.VIEW,
            actor_id=principal.actor_id,
            payload={"event_id": str(event.id)},
            event_id=event.id,
        )
    return event
