"""Threshold configuration and detection (REQ-03, AC-03)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.db.uow import UnitOfWork
from app.modules.metrics.models import MetricType
from app.modules.thresholds.models import Direction, ThresholdVersion
from app.modules.thresholds.repository import ThresholdRepository

# Upper bound used to ask for "the latest version" through in_force().
_END_OF_TIME = datetime(9999, 1, 1, tzinfo=UTC)


class ThresholdConfigError(ValueError):
    """A threshold version would violate the configuration rules."""


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


def add_threshold_version(
    uow: UnitOfWork,
    *,
    system_id: UUID,
    metric_name: str,
    metric_type: MetricType,
    direction: Direction,
    limit_value: Decimal,
    valid_from: datetime,
    changed_by: UUID | None,
) -> ThresholdVersion:
    """Configure a threshold by appending a new version (Model 2: insert-only, never updated).

    Section 5 defines no configuration endpoint, so this is the supported entry point for
    fixtures (/v1/test/reset) and for a future admin endpoint (D-07). Rules:
    - the system must exist;
    - limit_value must be finite;
    - valid_from must be timezone-aware and later than the current latest version, so history
      is only ever appended to (a back-dated version would change how late samples are judged);
    - metric_type must match earlier versions of the same metric.
    Runs inside its own unit of work.
    """
    if valid_from.tzinfo is None:
        raise ThresholdConfigError("valid_from must be timezone-aware")
    if not limit_value.is_finite():
        raise ThresholdConfigError("limit_value must be finite")

    with uow:
        if uow.systems.get(system_id) is None:
            raise ThresholdConfigError(f"unknown system_id {system_id}")
        latest = uow.thresholds.in_force(system_id, metric_name, _END_OF_TIME)
        if latest is not None:
            if valid_from <= latest.valid_from:
                raise ThresholdConfigError(
                    f"valid_from must be after the latest version ({latest.valid_from.isoformat()})"
                )
            if latest.metric_type != metric_type:
                raise ThresholdConfigError(
                    f"{metric_name} is configured as {latest.metric_type}, not {metric_type}"
                )
        threshold = ThresholdVersion(
            id=uuid4(),
            system_id=system_id,
            metric_name=metric_name,
            metric_type=MetricType(metric_type).value,
            direction=Direction(direction).value,
            limit_value=limit_value,
            valid_from=valid_from.astimezone(UTC),
            changed_by=changed_by,
        )
        uow.thresholds.add(threshold)
    return threshold
