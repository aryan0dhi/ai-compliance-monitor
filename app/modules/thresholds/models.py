"""threshold_version: versioned threshold configuration per system and metric (REQ-03)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in, check_not_nan
from app.modules.metrics.models import MetricType


class Direction(StrEnum):
    """Which side of the limit is a breach (docs/spec-decisions.md D-01).

    ABOVE suits drift-style metrics (drift_score > 0.30), BELOW suits performance-style metrics
    (accuracy < 0.70, Model 5 Ex-1). Comparison is strict: a value exactly at the limit never
    breaches (AC-03 boundary).
    """

    ABOVE = "above"
    BELOW = "below"

    def breaches(self, value: Decimal, limit: Decimal) -> bool:
        if self is Direction.ABOVE:
            return value > limit
        return value < limit


class ThresholdVersion(Base):
    """Rows are never updated: a threshold change inserts a new row with a later valid_from.

    The threshold in force for a sample is the row for (system_id, metric_name) with the greatest
    valid_from <= the sample's sampled_at. Flagged events reference the exact row that was
    breached, so history stays reconstructable.
    """

    __tablename__ = "threshold_version"
    __table_args__ = (
        UniqueConstraint(
            "system_id", "metric_name", "valid_from", name="uq_threshold_version_metric_valid_from"
        ),
        check_in("metric_type", MetricType, name="metric_type"),
        check_in("direction", Direction, name="direction"),
        check_not_nan("limit_value", name="limit_value_not_nan"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    system_id: Mapped[UUID] = mapped_column(ForeignKey("monitored_system.id", ondelete="RESTRICT"))
    metric_name: Mapped[str]
    # Model 2 calls this `kind` (perf | drift); renamed to match the API's metric_type.
    metric_type: Mapped[str]
    # Not in Model 2 (D-01).
    direction: Mapped[str]
    limit_value: Mapped[Decimal]
    valid_from: Mapped[datetime]
    # Model 2: user FK (REQ-14). No FK yet because the users table belongs to the auth module;
    # the auth owner adds the constraint in their migration. Null for seeded fixtures.
    changed_by: Mapped[UUID | None]
