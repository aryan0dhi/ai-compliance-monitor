"""Review: the Art. 3(49) harm fields of flagged events (REQ-07, B-07) and their dispositions
(REQ-08a/b, REQ-09, B-08, B-09).

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


class DispositionTier(StrEnum):
    """Article 73 tiers (pod interface DispositionTier). NOT_SERIOUS closes the event (REQ-08b)."""

    DEATH = "DEATH"
    CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD = "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD"
    OTHER_SERIOUS = "OTHER_SERIOUS"
    NOT_SERIOUS = "NOT_SERIOUS"

    @property
    def is_serious(self) -> bool:
        return self is not DispositionTier.NOT_SERIOUS


class Disposition(Base):
    """One tier assignment by a reviewer (REQ-08a/b). Insert-only: a revision is a new row (B-09).

    Primary key `(event_id, seq)`: `seq` numbers an event's dispositions from 1, and the latest
    row is the event's current tier. Every row after the first records the tier it replaced in
    `previous_tier`.
    """

    __tablename__ = "disposition"
    __table_args__ = (
        check_in("tier", DispositionTier, name="tier"),
        check_in("previous_tier", DispositionTier, name="previous_tier"),
        CheckConstraint("seq > 0", name="seq_positive"),
        # B-09: the first disposition replaces nothing; every revision names what it replaced.
        CheckConstraint("(seq = 1) = (previous_tier IS NULL)", name="previous_tier_matches_seq"),
        # B-08 / REQ-08b: a not-serious determination always carries its reason.
        CheckConstraint(
            f"tier <> '{DispositionTier.NOT_SERIOUS}' OR btrim(coalesce(rationale, '')) <> ''",
            name="not_serious_has_rationale",
        ),
        # B-10: every serious tier has a deadline; NOT_SERIOUS has none.
        CheckConstraint(
            f"(tier = '{DispositionTier.NOT_SERIOUS}') = (deadline_utc IS NULL)",
            name="deadline_matches_tier",
        ),
    )

    event_id: Mapped[UUID] = mapped_column(
        ForeignKey("event.id", ondelete="RESTRICT"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    tier: Mapped[str]
    previous_tier: Mapped[str | None]
    reviewer_id: Mapped[UUID] = mapped_column(ForeignKey("app_user.id", ondelete="RESTRICT"))
    rationale: Mapped[str | None]
    # core.clock time of the determination.
    recorded_at: Mapped[datetime]
    # The Article 73 deadline in force after this disposition (B-10, review.deadlines). The
    # latest row's value is the event's deadline_utc.
    deadline_utc: Mapped[datetime | None]
