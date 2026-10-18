"""Data access for webhook, notification and notification_delivery."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.modules.notifications.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    Webhook,
)


class NotificationRepository(Protocol):
    def add(self, notification: Notification) -> None: ...

    def get(self, notification_id: UUID) -> Notification | None: ...

    def undispatched(self, limit: int) -> list[Notification]:
        """Notifications not yet fanned out to any webhook, oldest first."""
        ...

    def add_delivery(self, delivery: NotificationDelivery) -> None: ...

    def get_delivery(
        self, notification_id: UUID, webhook_id: UUID
    ) -> NotificationDelivery | None: ...

    def due_deliveries(
        self, now: datetime, limit: int
    ) -> list[tuple[NotificationDelivery, Notification, Webhook]]:
        """Pending deliveries whose next attempt is due, earliest REQ-04 deadline first."""
        ...

    def count_undelivered_due_between(self, after: datetime, until: datetime) -> int:
        """Notifications whose deliver_by is in (after, until] and that no webhook received yet."""
        ...


class WebhookRepository(Protocol):
    def add(self, webhook: Webhook) -> None: ...

    def active(self) -> list[Webhook]: ...


class SqlNotificationRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, notification: Notification) -> None:
        self._session.add(notification)
        self._session.flush()

    def get(self, notification_id: UUID) -> Notification | None:
        return self._session.get(Notification, notification_id)

    def undispatched(self, limit: int) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.dispatched_at.is_(None))
            .order_by(Notification.created_at, Notification.id)
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    def add_delivery(self, delivery: NotificationDelivery) -> None:
        self._session.add(delivery)
        self._session.flush()

    def get_delivery(self, notification_id: UUID, webhook_id: UUID) -> NotificationDelivery | None:
        return self._session.get(NotificationDelivery, (notification_id, webhook_id))

    def due_deliveries(
        self, now: datetime, limit: int
    ) -> list[tuple[NotificationDelivery, Notification, Webhook]]:
        stmt = (
            select(NotificationDelivery, Notification, Webhook)
            .join(Notification, Notification.id == NotificationDelivery.notification_id)
            .join(Webhook, Webhook.id == NotificationDelivery.webhook_id)
            .where(
                NotificationDelivery.status == DeliveryStatus.PENDING.value,
                NotificationDelivery.next_attempt_at <= now,
                Webhook.disabled_at.is_(None),
            )
            .order_by(Notification.deliver_by, NotificationDelivery.next_attempt_at)
            .limit(limit)
        )
        return [(d, n, w) for d, n, w in self._session.execute(stmt).all()]

    def count_undelivered_due_between(self, after: datetime, until: datetime) -> int:
        delivered = exists().where(
            NotificationDelivery.notification_id == Notification.id,
            NotificationDelivery.status == DeliveryStatus.DELIVERED.value,
        )
        stmt = (
            select(func.count())
            .select_from(Notification)
            .where(Notification.deliver_by > after, Notification.deliver_by <= until, ~delivered)
        )
        return self._session.scalar(stmt) or 0


class SqlWebhookRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, webhook: Webhook) -> None:
        self._session.add(webhook)
        self._session.flush()

    def active(self) -> list[Webhook]:
        stmt = select(Webhook).where(Webhook.disabled_at.is_(None)).order_by(Webhook.created_at)
        return list(self._session.scalars(stmt))
