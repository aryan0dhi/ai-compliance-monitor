"""Production notifier: writes an outbox row in the caller's transaction (REQ-04, Q7.2)."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.config import get_settings
from app.modules.ledger.chain import require_json_safe
from app.modules.notifications.models import Notification, NotificationType
from app.modules.notifications.repository import SqlNotificationRepository


def notify_max_delay() -> timedelta:
    return timedelta(seconds=get_settings().notify_max_delay_s)


def build_notification(
    *,
    event_id: UUID,
    notification_type: str,
    payload: dict[str, Any],
    now: datetime,
    max_delay: timedelta,
) -> Notification:
    """An outbox row due for delivery within `max_delay` of now (REQ-04)."""
    require_json_safe(payload)
    return Notification(
        id=uuid4(),
        event_id=event_id,
        notification_type=NotificationType(notification_type).value,
        payload=payload,
        created_at=now,
        deliver_by=now + max_delay,
        dispatched_at=None,
    )


class OutboxNotifier:
    def __init__(
        self,
        clock_provider: Callable[[], Clock] = get_clock,
        max_delay_provider: Callable[[], timedelta] = notify_max_delay,
    ) -> None:
        # Resolved per call so the simulated clock and the configured delay are picked up.
        self._clock_provider = clock_provider
        self._max_delay_provider = max_delay_provider

    def enqueue(
        self,
        session: Session | None,
        *,
        notification_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        if session is None:
            raise RuntimeError("the notifier must be given the caller's database session")
        notification = build_notification(
            event_id=event_id,
            notification_type=notification_type,
            payload=payload,
            now=self._clock_provider().now(),
            max_delay=self._max_delay_provider(),
        )
        SqlNotificationRepository(session).add(notification)
