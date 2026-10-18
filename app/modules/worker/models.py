"""worker_heartbeat: liveness of the background worker, reported by /healthz (Q7.3)."""

from datetime import datetime

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    # hostname:pid of the active worker.
    worker_id: Mapped[str]
    # Real wall-clock time (liveness, not business time), so the test clock doesn't affect it.
    beat_at: Mapped[datetime]
