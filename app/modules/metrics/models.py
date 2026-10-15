"""metric_sample: raw performance and drift metrics (REQ-01; retained under REQ-05)."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, check_in, check_not_nan


class MetricType(StrEnum):
    """Section 5.2 MetricSample.metric_type."""

    PERFORMANCE = "performance"
    DRIFT = "drift"


class MetricSample(Base):
    __tablename__ = "metric_sample"
    __table_args__ = (
        check_in("metric_type", MetricType, name="metric_type"),
        check_not_nan("value", name="value_not_nan"),
    )

    # Primary key (system_id, metric_name, sampled_at) makes ingestion idempotent (Model 2).
    # What a repeated submission returns is still open: docs/spec-decisions.md D-04.
    system_id: Mapped[UUID] = mapped_column(
        ForeignKey("monitored_system.id", ondelete="RESTRICT"), primary_key=True
    )
    # Model 2 calls this column `metric`; renamed to match the API (D-02).
    metric_name: Mapped[str] = mapped_column(primary_key=True)
    # The API's `timestamp`: when the monitored system took the measurement.
    sampled_at: Mapped[datetime] = mapped_column(primary_key=True)
    # Not in Model 2; required by the API (D-02).
    metric_type: Mapped[str]
    value: Mapped[Decimal]
    # Reference to the source Evidently report/snapshot, if any.
    snapshot_ref: Mapped[str | None]
    # Server time (core.clock) when the sample arrived. Not in Model 2; used for AC-01 timing and
    # to tell late data from on-time data.
    received_at: Mapped[datetime]
