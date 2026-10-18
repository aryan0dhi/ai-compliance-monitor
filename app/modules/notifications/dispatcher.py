"""Notification delivery, run by the background worker (REQ-04, REQ-02).

Each pass:
1. fans undispatched outbox rows out to the active webhooks subscribed to their type;
2. snapshots the due deliveries and sends them OUTSIDE any transaction (a slow webhook must not
   hold the ledger lock);
3. records each result in its own transaction. A success also appends a NOTIFIED ledger entry and,
   for a monitoring gap, moves the event from GAP_OPEN to REVIEWER_NOTIFIED (Model 1).

Delivery is at-least-once: if the worker stops between sending and recording, the request is
sent again. Receivers deduplicate on `notification_id`.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.events.models import EventState, EventType
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.notifications.delivery import WebhookSender
from app.modules.notifications.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationType,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_BACKOFF = timedelta(minutes=5)


def retry_delay(attempts: int) -> timedelta:
    """10 s, 20 s, 40 s, ... capped at 5 minutes."""
    return min(timedelta(seconds=10 * 2 ** (attempts - 1)), MAX_BACKOFF)


def delivery_body(notification: Notification) -> bytes:
    """The JSON a webhook receives. Stable bytes, so the signature can be checked."""
    document = {
        "notification_id": str(notification.id),
        "notification_type": notification.notification_type,
        "event_id": str(notification.event_id),
        "created_at": notification.created_at.isoformat(),
        "deliver_by": notification.deliver_by.isoformat(),
        "event": notification.payload,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class DeliveryJob:
    """Plain-data snapshot of one due delivery, so sending needs no open transaction."""

    notification_id: UUID
    webhook_id: UUID
    url: str
    secret: str
    body: bytes


@dataclass
class DispatchResult:
    fanned_out: int = 0
    delivered: list[UUID] = field(default_factory=list)  # notification ids
    failed_attempts: int = 0
    gave_up: int = 0


def fan_out(uow: UnitOfWork, *, now: datetime) -> int:
    """Create delivery rows for undispatched notifications. Returns how many were dispatched."""
    dispatched = 0
    with uow:
        pending = uow.notifications.undispatched(BATCH_SIZE)
        if not pending:
            return 0
        webhooks = uow.webhooks.active()
        for notification in pending:
            targets = [w for w in webhooks if notification.notification_type in w.event_types]
            if not targets:
                continue  # stays undispatched until a subscribed webhook exists
            for webhook in targets:
                uow.notifications.add_delivery(
                    NotificationDelivery(
                        notification_id=notification.id,
                        webhook_id=webhook.id,
                        status=DeliveryStatus.PENDING.value,
                        attempts=0,
                        next_attempt_at=now,
                    )
                )
            notification.dispatched_at = now
            dispatched += 1
    return dispatched


def claim_due(uow: UnitOfWork, *, now: datetime) -> list[DeliveryJob]:
    with uow:
        due = uow.notifications.due_deliveries(now, BATCH_SIZE)
        return [
            DeliveryJob(
                notification_id=n.id,
                webhook_id=w.id,
                url=w.url,
                secret=w.secret,
                body=delivery_body(n),
            )
            for _, n, w in due
        ]


def record_result(
    uow: UnitOfWork,
    job: DeliveryJob,
    *,
    ok: bool,
    status_code: int | None,
    error: str | None,
    now: datetime,
    ledger: LedgerWriter,
    max_attempts: int,
) -> DeliveryStatus | None:
    """Store one attempt's outcome. Returns the delivery's new status (None if it vanished)."""
    with uow:
        delivery = uow.notifications.get_delivery(job.notification_id, job.webhook_id)
        notification = uow.notifications.get(job.notification_id)
        if delivery is None or notification is None or delivery.status != DeliveryStatus.PENDING:
            return None  # e.g. removed by /v1/test/reset while the request was in flight
        delivery.attempts += 1
        delivery.last_attempt_at = now
        delivery.last_status_code = status_code

        if not ok:
            delivery.last_error = (error or "delivery failed")[:500]
            if delivery.attempts >= max_attempts:
                delivery.status = DeliveryStatus.FAILED.value
                logger.error(
                    "webhook delivery abandoned after %s attempts: %s",
                    delivery.attempts,
                    delivery.last_error,
                    extra={"event_id": str(notification.event_id)},
                )
            else:
                delivery.next_attempt_at = now + retry_delay(delivery.attempts)
            return DeliveryStatus(delivery.status)

        delivery.status = DeliveryStatus.DELIVERED.value
        delivery.delivered_at = now
        delivery.last_error = None
        within_deadline = now <= notification.deliver_by
        payload: dict[str, object] = {
            "notification_id": str(notification.id),
            "notification_type": notification.notification_type,
            "webhook_id": str(job.webhook_id),
            "created_at": notification.created_at.isoformat(),
            "deliver_by": notification.deliver_by.isoformat(),
            "delivered_at": now.isoformat(),
            "within_max_delay": within_deadline,
            "attempts": delivery.attempts,
        }
        event = uow.events.get(notification.event_id)
        if (
            event is not None
            and event.type == EventType.MONITORING_GAP
            and event.state == EventState.GAP_OPEN
            and notification.notification_type == NotificationType.FLAG_NOTIFICATION
        ):
            event.state = EventState.REVIEWER_NOTIFIED.value
            payload["state"] = event.state
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.NOTIFIED,
            actor_id=None,
            payload=payload,
            event_id=notification.event_id,
        )
        if not within_deadline:
            logger.error(
                "REQ-04: notification delivered after its maximum delay (deliver_by %s)",
                notification.deliver_by.isoformat(),
                extra={"event_id": str(notification.event_id)},
            )
        return DeliveryStatus.DELIVERED


def dispatch_due(
    uow_factory: Callable[[], UnitOfWork],
    *,
    sender: WebhookSender,
    ledger: LedgerWriter,
    clock: Clock,
    max_attempts: int,
) -> DispatchResult:
    result = DispatchResult()
    result.fanned_out = fan_out(uow_factory(), now=clock.now())
    for job in claim_due(uow_factory(), now=clock.now()):
        outcome = sender.send(job.url, job.body, job.secret)
        status = record_result(
            uow_factory(),
            job,
            ok=outcome.ok,
            status_code=outcome.status_code,
            error=outcome.error,
            now=clock.now(),
            ledger=ledger,
            max_attempts=max_attempts,
        )
        if status is DeliveryStatus.DELIVERED:
            result.delivered.append(job.notification_id)
        elif status is DeliveryStatus.FAILED:
            result.gave_up += 1
        elif status is DeliveryStatus.PENDING:
            result.failed_attempts += 1
    return result
