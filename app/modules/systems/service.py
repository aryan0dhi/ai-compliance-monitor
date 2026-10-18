"""Monitored-system configuration (used by the admin CLI until D-07 adds an endpoint)."""

from uuid import UUID, uuid4

from app.db.uow import UnitOfWork
from app.modules.systems.models import (
    DEFAULT_GAP_MISSED_INTERVALS,
    DEFAULT_SAMPLING_INTERVAL_S,
    MonitoredSystem,
)


class SystemConfigError(ValueError):
    pass


def create_system(
    uow: UnitOfWork,
    *,
    name: str,
    sampling_interval_s: int = DEFAULT_SAMPLING_INTERVAL_S,
    gap_missed_intervals: int = DEFAULT_GAP_MISSED_INTERVALS,
    system_id: UUID | None = None,
) -> MonitoredSystem:
    if sampling_interval_s <= 0 or gap_missed_intervals <= 0:
        raise SystemConfigError("sampling interval and gap intervals must be positive")
    system = MonitoredSystem(
        id=system_id or uuid4(),
        name=name,
        sampling_interval_s=sampling_interval_s,
        gap_missed_intervals=gap_missed_intervals,
    )
    with uow:
        if uow.systems.get(system.id) is not None:
            raise SystemConfigError(f"system {system.id} already exists")
        uow.systems.add(system)
    return system
