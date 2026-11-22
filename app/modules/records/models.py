"""record_key: one wrapped data key per event that holds personal data (REQ-16, Model 2)."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RecordKey(Base):
    """Created the first time personal data is written for an event; never deleted.

    Destroying the key (REQ-16) sets wrapped_key to NULL and destroyed_at to the service-clock
    time. The row stays, so the event keeps its record of when its personal data was erased.
    """

    __tablename__ = "record_key"
    __table_args__ = (
        CheckConstraint(
            "(wrapped_key IS NULL) = (destroyed_at IS NOT NULL)", name="destroyed_has_no_key"
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("event.id", ondelete="RESTRICT"), primary_key=True
    )
    # nonce || AES-256-GCM(master key, data key), bound to event_id (records.crypto).
    wrapped_key: Mapped[bytes | None]
    created_at: Mapped[datetime]
    destroyed_at: Mapped[datetime | None]
