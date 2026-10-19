"""AC-01 (REQ-01): one hour at the default 5-min interval, every sample 300 s ± 10 s, no gaps.

A baseline sample, then 12 more at 5-minute steps, ingested on a FixedClock with the gap sweeper
run just before each arrival (the longest silence the worker could see). The stored received_at
log is then checked with the AC-01 cadence check (D-17).
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.modules.events.models import EventType
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.monitoring_gaps.service import sweep_gaps
from tests.contract.cadence import CadenceViolation, cadence_violations
from tests.fakes import FixedClock, RecordingLedger, RecordingNotifier, system_principal

START = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
INTERVAL = timedelta(seconds=300)
SAMPLES_AFTER_BASELINE = 12  # 12 x 5 min = 1 hour


def schedule(offsets: dict[int, timedelta] | None = None) -> list[datetime]:
    """Arrival times START + i * 5 min, with sample i moved by offsets[i]."""
    offsets = offsets or {}
    return [
        START + i * INTERVAL + offsets.get(i, timedelta())
        for i in range(SAMPLES_AFTER_BASELINE + 1)
    ]


def run_hour(uow, arrivals: list[datetime]) -> list[datetime]:
    """Ingest a sample at each arrival time; return the stored received_at log."""
    system_id = uow.seed_system()
    ledger, notifier = RecordingLedger(), RecordingNotifier()
    for at in arrivals:
        clock = FixedClock(at)
        sweep_gaps(lambda: uow, ledger=ledger, notifier=notifier, clock=clock)
        ingest_metric(
            MetricSubmission(
                system_id=system_id,
                metric_name="drift_score",
                metric_type=MetricType.DRIFT,
                value=Decimal("0.1"),
                sampled_at=at,
            ),
            principal=system_principal(system_id),
            uow=uow,
            ledger=ledger,
            notifier=notifier,
            clock=clock,
        )
    end = FixedClock(arrivals[-1] + INTERVAL)
    sweep_gaps(lambda: uow, ledger=ledger, notifier=notifier, clock=end)
    return [s.received_at for s in uow.metrics.rows.values() if s.system_id == system_id]


def gap_events(uow) -> list:
    return [e for e in uow.events.rows.values() if e.type == EventType.MONITORING_GAP]


def test_one_hour_on_schedule_has_no_cadence_violation_and_no_gap(uow):
    log = run_hour(uow, schedule())
    assert len(log) == SAMPLES_AFTER_BASELINE + 1
    assert cadence_violations(log) == []
    assert gap_events(uow) == []


@pytest.mark.parametrize(
    "offset",
    [timedelta(seconds=10), timedelta(seconds=-10)],
    ids=["10s-late", "10s-early"],
)
def test_sample_10s_off_still_passes(uow, offset):
    log = run_hour(uow, schedule({6: offset}))
    assert cadence_violations(log) == []
    assert gap_events(uow) == []


@pytest.mark.parametrize(
    "offset",
    [
        timedelta(seconds=10, microseconds=1),
        timedelta(seconds=11),
        timedelta(seconds=-11),
    ],
    ids=["10s+1us-late", "11s-late", "11s-early"],
)
def test_sample_more_than_10s_off_is_a_cadence_violation_not_a_gap(uow, offset):
    log = run_hour(uow, schedule({6: offset}))
    # Moving sample 6 stretches one interval and shrinks the next by the same amount.
    assert cadence_violations(log) == [
        CadenceViolation(index=6, interval=INTERVAL + offset),
        CadenceViolation(index=7, interval=INTERVAL - offset),
    ]
    assert gap_events(uow) == []  # D-17: the service opens a gap only after 3 missed intervals


class TestCadenceCheck:
    def test_too_few_samples_have_no_intervals(self):
        assert cadence_violations([]) == []
        assert cadence_violations([START]) == []

    def test_log_order_does_not_matter(self):
        assert cadence_violations(reversed(schedule())) == []

    def test_a_missing_sample_is_one_double_interval(self):
        log = [t for i, t in enumerate(schedule()) if i != 6]
        assert cadence_violations(log) == [CadenceViolation(index=6, interval=2 * INTERVAL)]

    def test_interval_and_tolerance_are_parameters(self):
        log = [START, START + timedelta(seconds=65)]
        assert cadence_violations(log, interval_s=60, tolerance_s=5) == []
        assert cadence_violations(log, interval_s=60, tolerance_s=4) != []
