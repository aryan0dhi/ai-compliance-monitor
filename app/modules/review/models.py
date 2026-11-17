"""Review: the Art. 3(49) harm fields of flagged events (REQ-07, B-07).

HarmFieldName is also used by monitored_system.harm_fields (D-20).
"""

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in


class HarmFieldName(StrEnum):
    """The four Art. 3(49) consequences a reviewer confirms for every flagged event (REQ-07)."""

    HEALTH_SAFETY = "health_safety"
    CRITICAL_INFRASTRUCTURE = "critical_infrastructure"
    FUNDAMENTAL_RIGHTS = "fundamental_rights"
    PROPERTY_ENVIRONMENT = "property_environment"


class HarmFieldStatus(StrEnum):
    """B-07: PENDING until a reviewer confirms the field or marks it not applicable (REQ-08a)."""

    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class HarmField(Base):
    """One Art. 3(49) field of one breach event (REQ-07, B-07). Four rows per breach event.

    Created PENDING in the flagging transaction; the disposition workflow (#38) confirms them.
    Monitoring-gap events never get rows (INV-12).
    """

    __tablename__ = "harm_field"
    __table_args__ = (
        check_in("name", HarmFieldName, name="name"),
        check_in("status", HarmFieldStatus, name="status"),
        # A field is confirmed (or marked N/A) by a reviewer at a time, or not at all.
        CheckConstraint(
            f"(status = '{HarmFieldStatus.PENDING}') = (confirmed_at IS NULL)",
            name="confirmed_at_matches_status",
        ),
        CheckConstraint(
            f"status <> '{HarmFieldStatus.PENDING}' OR confirmed_by IS NULL",
            name="pending_has_no_reviewer",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("event.id", ondelete="RESTRICT"), primary_key=True
    )
    name: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(
        default=HarmFieldStatus.PENDING.value, server_default=HarmFieldStatus.PENDING.value
    )
    # Drawn from the event's breach record for the fields its system lists (D-20); null otherwise.
    # Never changed after flagging.
    prefill_evidence: Mapped[str | None]
    # The reviewer's own evidence when confirming (DispositionIn.harm_fields, #38).
    evidence: Mapped[str | None]
    confirmed_by: Mapped[UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="RESTRICT")
    )
    confirmed_at: Mapped[datetime | None]
