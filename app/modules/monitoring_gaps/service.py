"""Monitoring-gap detection (REQ-02, AC-02, Model 5 Ex-3), run by the background worker.

A system that has reported before is in a gap once
    now - last_seen_at >= gap_missed_intervals * sampling_interval_s
(3 x 5 min by default). The boundary is inclusive: Ex-3 detects the gap at exactly 10:15 after the
last sample at 10:00, and AC-02 says withholding exactly 3 intervals triggers while 2 must not
(docs/spec-decisions.md D-11).

Opening a gap creates a monitoring_gap event (GAP_OPEN), its GAP_OPENED ledger entry and a
reviewer notification in one transaction; the notification shares REQ-04's maximum delay. When
data resumes the event moves to GAP_CLOSED with the resumption time, so the gap duration is known
(Ex-3: 1 h 40 min). Gap events never enter review (INV-12; enforced by ck_event_state_matches_type).
Systems that have never sent a sample are not monitored yet and can't be in a gap.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.events.models import (
    OPEN_GAP_STATES,
    Event,
    EventState,
    EventType,
    retain_until_for,
)
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.notifications.interface import NotificationType, Notifier
from app.modules.systems.models import MonitoredSystem

logger = logging.getLogger(__name__)


@dataclass
class GapSweepResult:
    opened: list[UUID] = field(default_factory=list)
    closed: list[UUID] = field(default_factory=list)


def gap_detected(system: MonitoredSystem, now: datetime) -> bool:
    if system.last_seen_at is None:
        return False
    return now - system.last_seen_at >= system.gap_timeout


def gap_payload(event: Event, system: MonitoredSystem) -> dict[str, Any]:
    """JSON-safe snapshot of a gap event, for its ledger entries and the reviewer notification."""
    payload: dict[str, Any] = {
        "event_id": str(event.id),
        "type": event.type,
        "state": event.state,
        "system_id": str(event.system_id),
        "system_name": system.name,
        "sampling_interval_s": system.sampling_interval_s,
        "gap_missed_intervals": system.gap_missed_intervals,
        "gap_started_at": event.gap_started_at.isoformat() if event.gap_started_at else None,
        "gap_ended_at": event.gap_ended_at.isoformat() if event.gap_ended_at else None,
        "detected_at": event.detected_at.isoformat(),
        "retain_until": event.retain_until.isoformat(),
    }
    if event.gap_started_at and event.gap_ended_at:
        payload["gap_duration_s"] = int((event.gap_ended_at - event.gap_started_at).total_seconds())
    return payload


def open_gap_if_due(
    uow: UnitOfWork,
    system_id: UUID,
    *,
    now: datetime,
    ledger: LedgerWriter,
    notifier: Notifier,
) -> Event | None:
    """Create a gap event for the system if it is (still) in a gap and has none open."""
    with uow:
        # The lock waits for an ingestion that is committing right now, then re-reads
        # last_seen_at, so a sample that arrives at the boundary can't race a gap into existence.
        system = uow.systems.get_for_update(system_id)
        if system is None or not gap_detected(system, now):
            return None
        if uow.events.open_gap_for_system(system_id) is not None:
            return None
        event = Event(
            id=uuid4(),
            type=EventType.MONITORING_GAP.value,
            state=EventState.GAP_OPEN.value,
            system_id=system_id,
            detected_at=now,
            retain_until=retain_until_for(now),
            gap_started_at=system.last_seen_at,
        )
        uow.events.add(event)
        payload = gap_payload(event, system)
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.GAP_OPENED,
            actor_id=None,
            payload=payload,
            event_id=event.id,
        )
        notifier.enqueue(
            uow.session,
            notification_type=NotificationType.FLAG_NOTIFICATION,
            event_id=event.id,
            payload=payload,
        )
    logger.warning(
        "monitoring gap opened: no data since %s",
        payload["gap_started_at"],
        extra={"event_id": str(event.id), "system_id": str(system_id)},
    )
    return event


def close_gap_if_resumed(uow: UnitOfWork, event_id: UUID, *, ledger: LedgerWriter) -> Event | None:
    """Close an open gap once a new sample has arrived since it started."""
    with uow:
        event = uow.events.get_for_update(event_id)
        if event is None or event.state not in OPEN_GAP_STATES or event.gap_started_at is None:
            return None
        resumed_at = uow.metrics.first_received_after(event.system_id, event.gap_started_at)
        if resumed_at is None:
            return None
        system = uow.systems.get(event.system_id)
        if system is None:  # pragma: no cover - RESTRICT FK keeps the system
            return None
        event.state = EventState.GAP_CLOSED.value
        event.gap_ended_at = resumed_at
        payload = gap_payload(event, system)
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.GAP_CLOSED,
            actor_id=None,
            payload=payload,
            event_id=event.id,
        )
    logger.info(
        "monitoring gap closed after %s s",
        payload["gap_duration_s"],
        extra={"event_id": str(event_id), "system_id": str(event.system_id)},
    )
    return event


def sweep_gaps(
    uow_factory: Callable[[], UnitOfWork],
    *,
    ledger: LedgerWriter,
    notifier: Notifier,
    clock: Clock,
) -> GapSweepResult:
    """Close gaps whose data resumed, then open gaps for systems that went quiet.

    Each system gets its own transaction, so one failure doesn't block the others.
    """
    result = GapSweepResult()
    now = clock.now()
    with uow_factory() as uow:
        open_gaps = [(e.id, e.system_id) for e in uow.events.open_gaps()]
        candidates = [s.id for s in uow.systems.list_reporting() if gap_detected(s, now)]

    still_open: set[UUID] = set()
    for event_id, system_id in open_gaps:
        try:
            closed = close_gap_if_resumed(uow_factory(), event_id, ledger=ledger)
        except Exception:
            logger.exception("closing a monitoring gap failed", extra={"event_id": str(event_id)})
            closed = None
        if closed is not None:
            result.closed.append(event_id)
        else:
            still_open.add(system_id)

    for system_id in candidates:
        if system_id in still_open:
            continue
        try:
            event = open_gap_if_due(
                uow_factory(), system_id, now=now, ledger=ledger, notifier=notifier
            )
        except Exception:
            logger.exception("opening a monitoring gap failed", extra={"system_id": str(system_id)})
            continue
        if event is not None:
            result.opened.append(event.id)
    return result
