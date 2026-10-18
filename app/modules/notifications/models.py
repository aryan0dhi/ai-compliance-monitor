"""webhook, notification (the outbox) and notification_delivery (REQ-04, REQ-02).

A `notification` row is written in the same transaction as the event that needs it (Q7.2
Decision 1). The worker fans it out to every active webhook subscribed to its type
(`notification_delivery`, one row per webhook) and delivers each with retries.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in


class NotificationType(StrEnum):
    """Matches the POST /v1/webhooks event_types (Phase 2, Section 5.1)."""

    FLAG_NOTIFICATION = "flag_notification"
    REMINDER = "reminder"


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"  # gave up after webhook_max_attempts


_TYPES_ARRAY = "ARRAY[" + ", ".join(f"'{t}'" for t in NotificationType) + "]::text[]"


class Webhook(Base):
    __tablename__ = "webhook"
    __table_args__ = (
        CheckConstraint(
            f"cardinality(event_types) > 0 AND event_types <@ {_TYPES_ARRAY}",
            name="event_types_known",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    url: Mapped[str]
    event_types: Mapped[list[str]] = mapped_column(ARRAY(Text))
    # HMAC-SHA256 key for the X-Signature-SHA256 header. Returned once at registration; stored
    # in clear because signing needs it.
    secret: Mapped[str]
    created_by: Mapped[UUID | None]
    created_at: Mapped[datetime]
    disabled_at: Mapped[datetime | None]


class Notification(Base):
    __tablename__ = "notification"
    __table_args__ = (
        check_in("notification_type", NotificationType, name="notification_type"),
        CheckConstraint("deliver_by >= created_at", name="deliver_by_after_created"),
        Index("ix_notification_event_id", "event_id"),
        # The worker's fan-out query: notifications not yet handed to any webhook.
        Index(
            "ix_notification_undispatched",
            "created_at",
            postgresql_where=text("dispatched_at IS NULL"),
        ),
        Index("ix_notification_deliver_by", "deliver_by"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    event_id: Mapped[UUID] = mapped_column(ForeignKey("event.id", ondelete="RESTRICT"))
    notification_type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # core.clock time of enqueueing: the detection time for flag notifications.
    created_at: Mapped[datetime]
    # REQ-04: created_at + NOTIFY_MAX_DELAY_S. Delivery after this is logged as a missed deadline.
    deliver_by: Mapped[datetime]
    # When delivery rows were created. NULL while no webhook is subscribed, so a webhook
    # registered later still receives it.
    dispatched_at: Mapped[datetime | None]


class NotificationDelivery(Base):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        check_in("status", DeliveryStatus, name="status"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint(
            f"(status = '{DeliveryStatus.DELIVERED}') = (delivered_at IS NOT NULL)",
            name="delivered_at_iff_delivered",
        ),
        # The worker's delivery query: pending attempts that are due.
        Index(
            "ix_notification_delivery_due",
            "next_attempt_at",
            postgresql_where=text(f"status = '{DeliveryStatus.PENDING}'"),
        ),
        Index("ix_notification_delivery_webhook_id", "webhook_id"),
    )

    notification_id: Mapped[UUID] = mapped_column(
        ForeignKey("notification.id", ondelete="RESTRICT"), primary_key=True
    )
    webhook_id: Mapped[UUID] = mapped_column(
        ForeignKey("webhook.id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str]
    attempts: Mapped[int]
    next_attempt_at: Mapped[datetime]
    last_attempt_at: Mapped[datetime | None]
    delivered_at: Mapped[datetime | None]
    last_status_code: Mapped[int | None]
    last_error: Mapped[str | None]
