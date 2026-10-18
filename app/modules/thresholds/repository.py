"""Data access for threshold_version."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.modules.thresholds.models import ThresholdVersion


class ThresholdRepository(Protocol):
    def in_force(self, system_id: UUID, metric_name: str, at: datetime) -> ThresholdVersion | None:
        """The version with the greatest valid_from <= `at` for (system_id, metric_name)."""
        ...

    def add(self, threshold: ThresholdVersion) -> None:
        """Insert a new version. Versions are never updated or deleted."""
        ...


def build_in_force_query(system_id: UUID, metric_name: str, at: datetime) -> Select:
    return (
        select(ThresholdVersion)
        .where(
            ThresholdVersion.system_id == system_id,
            ThresholdVersion.metric_name == metric_name,
            ThresholdVersion.valid_from <= at,
        )
        .order_by(ThresholdVersion.valid_from.desc())
        .limit(1)
    )


class SqlThresholdRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def in_force(self, system_id: UUID, metric_name: str, at: datetime) -> ThresholdVersion | None:
        return self._session.scalars(build_in_force_query(system_id, metric_name, at)).first()

    def add(self, threshold: ThresholdVersion) -> None:
        self._session.add(threshold)
        self._session.flush()
