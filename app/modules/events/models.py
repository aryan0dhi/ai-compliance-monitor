"""event: flagged threshold breaches and monitoring gaps (REQ-03 creation, REQ-05 storage).

This table is shared. Phase 3 (this module) only creates FLAGGED threshold_breach events. Other
modules extend it: the gap sweeper creates monitoring_gap events (REQ-02) and may add gap columns;
review, disposition, Art. 3(49) fields, escalations, reports and ledger entries live in their own
tables that reference event.id (Model 2).
"""

import calendar
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, Index, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in

# REQ-05: minimum retention for flagged events and their raw metric data.
RETENTION_MONTHS = 6


class EventType(StrEnum):
    """Section 5.2 Event.type. Model 2 uses BREACH | GAP (docs/spec-decisions.md D-03)."""

    THRESHOLD_BREACH = "threshold_breach"
    MONITORING_GAP = "monitoring_gap"


class EventState(StrEnum):
    """Section 5.2 Event.state. Model 1 uses different names (D-03); interim until pod agrees."""

    FLAGGED = "FLAGGED"
    UNDER_REVIEW = "UNDER_REVIEW"
    DISPOSITIONED = "DISPOSITIONED"
    CLOSED = "CLOSED"


def retain_until_for(detected_at: datetime) -> datetime:
    """detected_at + 6 calendar months in UTC, clamped to month end (matches PostgreSQL).

    Satisfies the ck_event_retain_until_min_6_months constraint. This only computes the stored
    value; nothing deletes or protects data based on it yet (retention enforcement is Phase 5).
    """
    if detected_at.tzinfo is None:
        raise ValueError("detected_at must be timezone-aware")
    detected_at = detected_at.astimezone(UTC)
    month_index = detected_at.month - 1 + RETENTION_MONTHS
    year = detected_at.year + month_index // 12
    month = month_index % 12 + 1
    day = min(detected_at.day, calendar.monthrange(year, month)[1])
    return detected_at.replace(year=year, month=month, day=day)


_BREACH_FIELDS = (
    "metric_name",
    "metric_timestamp",
    "observed_value",
    "threshold_version_id",
    "threshold_value",
)


class Event(Base):
    __tablename__ = "event"
    __table_args__ = (
        check_in("type", EventType, name="type"),
        check_in("state", EventState, name="state"),
        # REQ-03: a breach event records metric name, timestamp, observed value, threshold value
        # and system id. Gap events leave these null.
        CheckConstraint(
            f"type <> '{EventType.THRESHOLD_BREACH}' OR ("
            + " AND ".join(f"{c} IS NOT NULL" for c in _BREACH_FIELDS)
            + ")",
            name="breach_fields_present",
        ),
        # REQ-05 / INV-10: the stored retention date can never be shorter than six months.
        # Month arithmetic is done in UTC explicitly; plain `timestamptz + interval` would use the
        # session time zone and could disagree with retain_until_for() near month ends.
        CheckConstraint(
            f"retain_until >= ((detected_at AT TIME ZONE 'UTC') "
            f"+ interval '{RETENTION_MONTHS} months') AT TIME ZONE 'UTC'",
            name="retain_until_min_6_months",
        ),
        CheckConstraint(
            "evidence_bundle_hash IS NULL OR evidence_bundle_hash ~ '^[0-9a-f]{64}$'",
            name="evidence_bundle_hash_sha256",
        ),
        # The triggering sample must exist and cannot be deleted while the event exists (REQ-05).
        ForeignKeyConstraint(
            ["system_id", "metric_name", "metric_timestamp"],
            ["metric_sample.system_id", "metric_sample.metric_name", "metric_sample.sampled_at"],
            ondelete="RESTRICT",
            name="fk_event_triggering_sample",
        ),
        # At most one breach event per sample, so a duplicate submission can never create a
        # second event (D-04).
        Index(
            "uq_event_breach_per_sample",
            "system_id",
            "metric_name",
            "metric_timestamp",
            unique=True,
            postgresql_where=text(f"type = '{EventType.THRESHOLD_BREACH}'"),
        ),
        # GET /v1/events filters: system_id, state, type, since (detected_at).
        Index("ix_event_system_id_detected_at", "system_id", "detected_at"),
        Index("ix_event_state", "state"),
        Index("ix_event_type", "type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    type: Mapped[str]
    state: Mapped[str] = mapped_column(default=EventState.FLAGGED.value)
    system_id: Mapped[UUID] = mapped_column(ForeignKey("monitored_system.id", ondelete="RESTRICT"))

    # Breach-only fields (null for monitoring_gap events).
    metric_name: Mapped[str | None]
    # The triggering sample's sampled_at: REQ-03's "timestamp".
    metric_timestamp: Mapped[datetime | None]
    observed_value: Mapped[Decimal | None]
    threshold_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("threshold_version.id", ondelete="RESTRICT")
    )
    # Copy of threshold_version.limit_value at flag time, so the record is self-contained.
    threshold_value: Mapped[Decimal | None]

    # REQ-15: core.clock time when the event was flagged. Set once, never updated (INV-13).
    detected_at: Mapped[datetime]
    # REQ-05: earliest time the event and its evidence may be removed. Model 2 says `date`;
    # timestamptz avoids time-zone rounding. Storage only: not enforced by any job yet.
    retain_until: Mapped[datetime]
    # REQ-05: sha256 of the evidence bundle. Nullable until the bundle window is agreed (D-05).
    evidence_bundle_hash: Mapped[str | None]
