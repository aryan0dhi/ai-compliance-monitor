"""Notification interface.

Flagging an event must insert a notification outbox row in the SAME transaction as the event
(Phase 2, Q7.4); the background worker delivers it later (REQ-04).

Callers receive a `Notifier` through `get_notifier()` (a FastAPI dependency) and pass their
unit-of-work session to `enqueue`.

NOT IMPLEMENTED. The production notifier is `UnimplementedNotifier`, which raises
UnimplementedError. That aborts the caller's transaction, so an event can never be flagged without
a reviewer notification being queued. The notifications owner replaces `UnimplementedNotifier`
and keeps the `Notifier` signature.
"""

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import UnimplementedError


class NotificationType(StrEnum):
    """Matches the POST /v1/webhooks event_types (Phase 2, Section 5.1)."""

    FLAG_NOTIFICATION = "flag_notification"
    REMINDER = "reminder"


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


class UnimplementedNotifier:
    def enqueue(
        self,
        session: Session | None,
        *,
        notification_type: str,
        event_id: UUID,
        payload: dict[str, Any],
    ) -> None:
        raise UnimplementedError("Notification enqueue", "REQ-04", "notifications")


_notifier: Notifier = UnimplementedNotifier()


def get_notifier() -> Notifier:
    """FastAPI dependency returning the production notifier."""
    return _notifier
