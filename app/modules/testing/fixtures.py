"""Baseline fixtures reseeded by POST /v1/test/reset.

Taken from the specification's own examples so cross-team tests can use known values: the
Section 5.2 system id, Model 5's loan-scorer (accuracy below 0.70, Ex-1) and resume-ranker
(PSI drift above 0.30, Ex-2), and AC-03's drift > 0.3. The shared pod fixture format is still
open (docs/spec-decisions.md D-07); replace these once it is agreed.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from app.modules.metrics.models import MetricType
from app.modules.thresholds.models import Direction

# Thresholds apply to every sample timestamped after this.
FIXTURE_VALID_FROM = datetime(2000, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class FixtureThreshold:
    metric_name: str
    metric_type: MetricType
    direction: Direction
    limit_value: Decimal


@dataclass(frozen=True)
class FixtureSystem:
    id: UUID
    name: str
    sampling_interval_s: int
    gap_missed_intervals: int
    thresholds: tuple[FixtureThreshold, ...]


BASELINE_SYSTEMS: tuple[FixtureSystem, ...] = (
    FixtureSystem(
        id=UUID("550e8400-e29b-41d4-a716-446655440000"),
        name="loan-scorer",
        sampling_interval_s=300,
        gap_missed_intervals=3,
        thresholds=(
            FixtureThreshold("accuracy", MetricType.PERFORMANCE, Direction.BELOW, Decimal("0.70")),
            FixtureThreshold("drift_score", MetricType.DRIFT, Direction.ABOVE, Decimal("0.30")),
        ),
    ),
    FixtureSystem(
        id=UUID("550e8400-e29b-41d4-a716-446655440001"),
        name="resume-ranker",
        sampling_interval_s=300,
        gap_missed_intervals=3,
        thresholds=(FixtureThreshold("psi", MetricType.DRIFT, Direction.ABOVE, Decimal("0.30")),),
    ),
)
