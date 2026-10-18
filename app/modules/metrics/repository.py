"""Data access for metric_sample."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Insert, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.metrics.models import MetricSample


class MetricRepository(Protocol):
    def get(
        self, system_id: UUID, metric_name: str, sampled_at: datetime
    ) -> MetricSample | None: ...

    def add_if_absent(self, sample: MetricSample) -> bool:
        """Insert the sample unless one with the same (system_id, metric_name, sampled_at) exists.

        Returns True if inserted, False if a sample with that key already existed. Must be safe
        under concurrent submissions of the same key.
        """
        ...

    def first_received_after(self, system_id: UUID, after: datetime) -> datetime | None:
        """Earliest received_at later than `after`: when data resumed after a gap (REQ-02)."""
        ...


def build_insert_if_absent(sample: MetricSample) -> Insert:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING: race-safe idempotent insert on the PK."""
    return (
        insert(MetricSample)
        .values(
            system_id=sample.system_id,
            metric_name=sample.metric_name,
            sampled_at=sample.sampled_at,
            metric_type=sample.metric_type,
            value=sample.value,
            snapshot_ref=sample.snapshot_ref,
            received_at=sample.received_at,
        )
        .on_conflict_do_nothing(index_elements=["system_id", "metric_name", "sampled_at"])
        .returning(MetricSample.system_id)
    )


class SqlMetricRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, system_id: UUID, metric_name: str, sampled_at: datetime) -> MetricSample | None:
        return self._session.get(MetricSample, (system_id, metric_name, sampled_at))

    def add_if_absent(self, sample: MetricSample) -> bool:
        return self._session.execute(build_insert_if_absent(sample)).first() is not None

    def first_received_after(self, system_id: UUID, after: datetime) -> datetime | None:
        stmt = select(func.min(MetricSample.received_at)).where(
            MetricSample.system_id == system_id, MetricSample.received_at > after
        )
        return self._session.scalar(stmt)
