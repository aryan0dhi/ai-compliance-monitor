"""Article 73 deadline reminders (REQ-11, B-11, AC-12; D-24), queued by the background worker.

For a serious-tier event, the window runs from A (first serious disposition) to the deadline in
force. When the service clock reaches 50 %, 80 % and 100 % of it, one `reminder` outbox row is
queued per checkpoint; the dispatcher then delivers it once to each webhook subscribed to
`reminder` (at least once on the wire; receivers deduplicate on notification_id).

- Checkpoints are recomputed from the current deadline on every pass, so a revision that moves the
  deadline moves the checkpoints still to come. One that already went out is never sent again
  (`uq_notification_reminder_once`).
- A worker that was down, or a clock advanced past several checkpoints, queues every one that is
  due, in order.
- CLOSED events (NOT_SERIOUS) get none.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.events.models import EventState
from app.modules.events.service import event_payload
from app.modules.notifications.models import NotificationType
from app.modules.notifications.outbox import build_notification
from app.modules.review.models import DispositionTier

REMINDER_PERCENTS = (50, 80, 100)


def checkpoint(anchor: datetime, deadline: datetime, percent: int) -> datetime:
    """The time `percent` % of the window from A to the deadline has elapsed (to the µs)."""
    return anchor + (deadline - anchor) * percent / 100


def due_percents(anchor: datetime, deadline: datetime, now: datetime, sent: set[int]) -> list[int]:
    """Checkpoints reached by `now` that haven't been queued yet, in order."""
    return [
        p for p in REMINDER_PERCENTS if p not in sent and now >= checkpoint(anchor, deadline, p)
    ]


@dataclass
class ReminderSweepResult:
    queued: list[tuple[UUID, int]] = field(default_factory=list)  # (event_id, percent)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def queue_due_reminders(
    uow: UnitOfWork, event_id: UUID, *, now: datetime, max_delay: timedelta
) -> list[int]:
    """Queue the event's due reminders in one transaction. Returns the percents queued."""
    with uow:
        # Locked like a disposition, so a concurrent revision can't move the deadline under us.
        event = uow.events.get_for_update(event_id)
        if event is None or event.state != EventState.DISPOSITIONED:
            return []
        history = uow.dispositions.for_event(event_id)
        anchor = next((d.recorded_at for d in history if DispositionTier(d.tier).is_serious), None)
        deadline = history[-1].deadline_utc if history else None
        if anchor is None or deadline is None:
            return []
        due = due_percents(anchor, deadline, now, uow.notifications.reminder_percents(event_id))
        payload = {**event_payload(event), "deadline_utc": _iso(deadline)}
        for percent in due:
            notification = build_notification(
                event_id=event_id,
                notification_type=NotificationType.REMINDER,
                payload=payload,
                now=now,
                max_delay=max_delay,
            )
            notification.reminder_percent = percent
            notification.reminder_deadline_utc = deadline
            uow.notifications.add(notification)
    return due


def sweep_reminders(
    uow_factory: Callable[[], UnitOfWork], *, clock: Clock, max_delay: timedelta
) -> ReminderSweepResult:
    """One worker pass over every DISPOSITIONED event (one transaction per event)."""
    result = ReminderSweepResult()
    with uow_factory() as uow:
        event_ids = uow.events.ids_in_state(EventState.DISPOSITIONED)
    for event_id in event_ids:
        for percent in queue_due_reminders(
            uow_factory(), event_id, now=clock.now(), max_delay=max_delay
        ):
            result.queued.append((event_id, percent))
    return result
