"""Threshold detection (REQ-03, AC-03)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.modules.thresholds.models import Direction, ThresholdVersion
from app.modules.thresholds.repository import ThresholdRepository


def threshold_in_force(
    repo: ThresholdRepository, system_id: UUID, metric_name: str, at: datetime
) -> ThresholdVersion | None:
    """The threshold that applied when the sample was measured (`at` = the sample's timestamp).

    Using the measurement time, not the arrival time, means a late-arriving sample is judged
    against the threshold that was configured when it was taken.
    """
    return repo.in_force(system_id, metric_name, at)


def is_breach(threshold: ThresholdVersion, value: Decimal) -> bool:
    """Strict comparison in the threshold's direction; a value equal to the limit never breaches."""
    return Direction(threshold.direction).breaches(value, threshold.limit_value)
