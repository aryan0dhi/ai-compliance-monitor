"""REQ-03 threshold configuration (thresholds.service.add_threshold_version)."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from app.modules.metrics.models import MetricType
from app.modules.thresholds.models import Direction
from app.modules.thresholds.service import ThresholdConfigError, add_threshold_version
from tests.conftest import T0


def add(uow, system_id, **overrides):
    kwargs = {
        "system_id": system_id,
        "metric_name": "drift_score",
        "metric_type": MetricType.DRIFT,
        "direction": Direction.ABOVE,
        "limit_value": Decimal("0.30"),
        "valid_from": T0,
        "changed_by": None,
    }
    kwargs.update(overrides)
    return add_threshold_version(uow, **kwargs)


@pytest.fixture
def system_id(uow):
    return uow.seed_system()


def test_adds_version(uow, system_id):
    actor = uuid4()
    t = add(uow, system_id, changed_by=actor)
    assert uow.thresholds.rows == [t]
    assert (t.metric_type, t.direction, t.limit_value, t.changed_by) == (
        "drift",
        "above",
        Decimal("0.30"),
        actor,
    )
    assert uow.commits == 1


def test_valid_from_normalized_to_utc(uow, system_id):
    t = add(uow, system_id, valid_from=T0.astimezone(timezone(timedelta(hours=-4))))
    assert t.valid_from == T0 and t.valid_from.utcoffset() == timedelta(0)


def test_new_version_appends_and_takes_effect(uow, system_id):
    first = add(uow, system_id)
    second = add(uow, system_id, limit_value=Decimal("0.50"), valid_from=T0 + timedelta(days=1))
    assert uow.thresholds.rows == [first, second]  # original never modified
    assert first.limit_value == Decimal("0.30")
    assert uow.thresholds.in_force(system_id, "drift_score", T0 + timedelta(days=2)) is second


@pytest.mark.parametrize("delta", [timedelta(0), timedelta(seconds=-1)])
def test_back_dated_or_same_time_version_rejected(uow, system_id, delta):
    add(uow, system_id)
    with pytest.raises(ThresholdConfigError):
        add(uow, system_id, valid_from=T0 + delta)
    assert len(uow.thresholds.rows) == 1


def test_metric_type_must_stay_consistent(uow, system_id):
    add(uow, system_id)
    with pytest.raises(ThresholdConfigError):
        add(
            uow,
            system_id,
            metric_type=MetricType.PERFORMANCE,
            valid_from=T0 + timedelta(days=1),
        )


def test_direction_can_change_between_versions(uow, system_id):
    add(uow, system_id)
    t = add(uow, system_id, direction=Direction.BELOW, valid_from=T0 + timedelta(days=1))
    assert t.direction == "below"


@pytest.mark.parametrize("limit", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_limit_rejected(uow, system_id, limit):
    with pytest.raises(ThresholdConfigError):
        add(uow, system_id, limit_value=Decimal(limit))


def test_naive_valid_from_rejected(uow, system_id):
    with pytest.raises(ThresholdConfigError):
        add(uow, system_id, valid_from=datetime(2026, 9, 27))


def test_unknown_system_rejected(uow):
    with pytest.raises(ThresholdConfigError):
        add(uow, uuid4())
    assert uow.thresholds.rows == []


def test_invalid_enum_values_rejected(uow, system_id):
    with pytest.raises(ValueError):
        add(uow, system_id, direction="sideways")
