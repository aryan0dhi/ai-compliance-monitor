"""Notification interface.

Flagging an event must insert a notification outbox row in the SAME transaction as the event
(Phase 2, Q7.4); the background worker delivers it later.

NOT IMPLEMENTED. `enqueue` raises UnimplementedError, which aborts the caller's transaction, so an
event can never be flagged without a reviewer notification being queued.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import UnimplementedError


def enqueue(
    session: Session,
    *,
    notification_type: str,
    event_id: UUID,
    payload: dict[str, Any],
) -> None:
    """Queue a notification (e.g. "flag_notification") in the caller's transaction."""
    raise UnimplementedError("Notification enqueue", "REQ-04", "notifications")
