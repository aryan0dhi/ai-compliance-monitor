"""monitored_system: one row per monitored high-risk AI system (Model 2)."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

DEFAULT_SAMPLING_INTERVAL_S = 300  # REQ-01: default 5 minutes


class MonitoredSystem(Base):
    __tablename__ = "monitored_system"
    __table_args__ = (
        CheckConstraint("sampling_interval_s > 0", name="sampling_interval_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(unique=True)
    # REQ-01: configurable sampling interval. The gap sweeper (REQ-02) derives its timeout from it.
    sampling_interval_s: Mapped[int] = mapped_column(
        default=DEFAULT_SAMPLING_INTERVAL_S, server_default=str(DEFAULT_SAMPLING_INTERVAL_S)
    )
    # Updated by metric ingestion (REQ-01); read by the gap sweeper (REQ-02).
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
