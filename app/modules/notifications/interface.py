"""Notification interface.

Flagging an event must insert a notification outbox row in the SAME transaction as the event
(Phase 2, Q7.4); the background worker delivers it later (REQ-04).

Callers receive a `Notifier` through `get_notifier()` (a FastAPI dependency) and pass their
unit-of-work session to `enqueue`. The production notifier (`OutboxNotifier`) never sends anything
inline; if it fails, the caller's transaction rolls back, so an event can never be flagged without
a reviewer notification being queued.
"""

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.notifications.models import NotificationType
from app.modules.notifications.outbox import OutboxNotifier

__all__ = ["NotificationType", "Notifier", "get_notifier"]


class Notifier(Protocol):
    def enqueue(
        self,
        session: Session | None,
        *,
        notification_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        """Queue a notification in the caller's transaction."""
        ...


_notifier: Notifier = OutboxNotifier()


def get_notifier() -> Notifier:
    """FastAPI dependency returning the production notifier."""
    return _notifier
