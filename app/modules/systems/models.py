"""monitored_system: one row per monitored high-risk AI system (Model 2)."""

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.review.models import HarmFieldName

DEFAULT_SAMPLING_INTERVAL_S = 300  # REQ-01: default 5 minutes
DEFAULT_GAP_MISSED_INTERVALS = 3  # REQ-02: default timeout of 3 missed sampling intervals

_HARM_FIELDS_ARRAY = "ARRAY[" + ", ".join(f"'{h}'" for h in HarmFieldName) + "]::text[]"


class MonitoredSystem(Base):
    __tablename__ = "monitored_system"
    __table_args__ = (
        CheckConstraint("sampling_interval_s > 0", name="sampling_interval_positive"),
        CheckConstraint("gap_missed_intervals > 0", name="gap_missed_intervals_positive"),
        CheckConstraint(f"harm_fields <@ {_HARM_FIELDS_ARRAY}", name="harm_fields_known"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    # REQ-01: configurable sampling interval. The gap sweeper (REQ-02) derives its timeout from it.
    sampling_interval_s: Mapped[int] = mapped_column(
        default=DEFAULT_SAMPLING_INTERVAL_S, server_default=str(DEFAULT_SAMPLING_INTERVAL_S)
    )
    # REQ-02: configurable timeout, as a number of sampling intervals without new data.
    gap_missed_intervals: Mapped[int] = mapped_column(
        default=DEFAULT_GAP_MISSED_INTERVALS, server_default=str(DEFAULT_GAP_MISSED_INTERVALS)
    )
    # REQ-07 (D-20): the Art. 3(49) fields this system's use can affect. A breach prefills these
    # fields' evidence; the others start blank. Configuration, never inferred.
    harm_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), default=list, server_default=text("'{}'::text[]")
    )
    # Updated by metric ingestion (REQ-01); read by the gap sweeper (REQ-02).
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    @property
    def gap_timeout(self) -> timedelta:
        return timedelta(seconds=self.sampling_interval_s * self.gap_missed_intervals)
