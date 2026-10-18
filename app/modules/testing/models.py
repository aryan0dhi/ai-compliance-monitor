"""simulated_clock: the test environment's clock offset, shared by the API and the worker."""

from sqlalchemy import BigInteger, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SimulatedClockState(Base):
    """At most one row (id = 1). Absent means no offset. Unused outside the test environment."""

    __tablename__ = "simulated_clock"
    __table_args__ = (
        CheckConstraint("id = 1", name="single_row"),
        CheckConstraint("offset_seconds >= 0", name="offset_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    offset_seconds: Mapped[int] = mapped_column(BigInteger)
