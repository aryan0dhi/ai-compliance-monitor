"""Monitoring-gap detection (REQ-02, AC-02, Model 5 Ex-3, INV-12)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import UnimplementedError
from app.modules.events.models import GAP_STATES, EventState, EventType
from app.modules.ledger.interface import LedgerEntryType
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.monitoring_gaps.service import gap_detected, sweep_gaps
from app.modules.notifications.interface import NotificationType
from tests.fakes import (
    FixedClock,
    OutboxRecorder,
    RecordingLedger,
    RecordingNotifier,
    system_principal,
)

# Model 5 Ex-3: no snapshot from "loan-scorer" after 10:00 (interval 5 min).
LAST_DATA = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


@pytest.fixture
def system_id(uow):
    return uow.seed_system(last_seen_at=LAST_DATA)


def sweep(uow, clock, ledger=None, notifier=None):
    return sweep_gaps(
        lambda: uow,
        ledger=ledger or RecordingLedger(),
        notifier=notifier or RecordingNotifier(),
        clock=clock,
    )


def send_sample(uow, system_id, at: datetime):
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
        ledger=RecordingLedger(),
        notifier=RecordingNotifier(),
        clock=FixedClock(at),
    )


class TestAc02Boundaries:
    @pytest.mark.parametrize(
        ("elapsed", "gap"),
        [
            (timedelta(minutes=5), False),  # 1 missed interval
            (timedelta(minutes=10), False),  # AC-02: withholding 2 intervals must NOT trigger
            (timedelta(minutes=15) - timedelta(microseconds=1), False),
            (timedelta(minutes=15), True),  # AC-02: exactly 3 intervals; Ex-3 detects at 10:15
            (timedelta(hours=2), True),
        ],
    )
    def test_three_missed_intervals(self, uow, system_id, elapsed, gap):
        result = sweep(uow, FixedClock(LAST_DATA + elapsed))
        assert bool(result.opened) is gap

    def test_timeout_is_configurable_per_system(self, uow):
        system_id = uow.seed_system(
            sampling_interval_s=60, gap_missed_intervals=5, last_seen_at=LAST_DATA
        )
        system = uow.systems.get(system_id)
        assert not gap_detected(system, LAST_DATA + timedelta(minutes=4, seconds=59))
        assert gap_detected(system, LAST_DATA + timedelta(minutes=5))

    def test_system_that_never_reported_has_no_gap(self, uow):
        uow.seed_system(last_seen_at=None)
        assert sweep(uow, FixedClock(LAST_DATA + timedelta(days=30))).opened == []


class TestEx3Trace:
    def test_gap_opens_notifies_and_closes_with_duration(self, uow, system_id):
        ledger = RecordingLedger()
        clock = FixedClock(datetime(2026, 9, 28, 10, 15, tzinfo=UTC))
        notifier = OutboxRecorder(uow.notifications, clock)

        # 10:15: sweeper finds 3 missed intervals -> GAP event -> reviewer notification queued.
        (event_id,) = sweep(uow, clock, ledger, notifier).opened
        event = uow.events.get(event_id)
        assert (event.type, event.state) == (EventType.MONITORING_GAP, EventState.GAP_OPEN)
        assert event.detected_at == clock.now() and event.gap_started_at == LAST_DATA
        assert event.gap_ended_at is None
        assert event.retain_until == datetime(2027, 3, 28, 10, 15, tzinfo=UTC)  # REQ-05

        (notification,) = uow.notifications.rows.values()
        assert notification.event_id == event_id
        assert notification.notification_type == NotificationType.FLAG_NOTIFICATION
        # REQ-02: same maximum delay as REQ-04 (15 minutes by default).
        assert notification.deliver_by == clock.now() + timedelta(minutes=15)
        assert notification.payload["type"] == "monitoring_gap"
        assert notification.payload["gap_started_at"] == LAST_DATA.isoformat()

        opened = ledger.calls[0]
        assert opened["entry_type"] == LedgerEntryType.GAP_OPENED
        assert opened["event_id"] == event_id and opened["actor_id"] is None

        # Repeated sweeps while still quiet: no second gap and no second notification.
        clock.advance(minutes=30)
        assert sweep(uow, clock, ledger, notifier).opened == []
        assert len(uow.notifications.rows) == 1

        # 11:40: data resumes; the next sweep closes the gap with a 1 h 40 m duration.
        send_sample(uow, system_id, datetime(2026, 9, 28, 11, 40, tzinfo=UTC))
        clock.current = datetime(2026, 9, 28, 11, 40, 5, tzinfo=UTC)
        result = sweep(uow, clock, ledger, notifier)
        assert result.closed == [event_id] and result.opened == []
        assert event.state == EventState.GAP_CLOSED
        assert event.gap_ended_at == datetime(2026, 9, 28, 11, 40, tzinfo=UTC)
        closed = ledger.calls[-1]
        assert closed["entry_type"] == LedgerEntryType.GAP_CLOSED
        assert closed["payload"]["gap_duration_s"] == 100 * 60

    def test_closing_uses_the_first_new_sample_not_the_latest(self, uow, system_id):
        clock = FixedClock(LAST_DATA + timedelta(minutes=15))
        (event_id,) = sweep(uow, clock).opened
        first = LAST_DATA + timedelta(hours=1)
        send_sample(uow, system_id, first)
        send_sample(uow, system_id, first + timedelta(minutes=5))
        clock.current = first + timedelta(minutes=6)
        sweep(uow, clock)
        assert uow.events.get(event_id).gap_ended_at == first

    def test_a_new_gap_can_open_after_one_closes(self, uow, system_id):
        clock = FixedClock(LAST_DATA + timedelta(minutes=15))
        (first,) = sweep(uow, clock).opened
        resumed = LAST_DATA + timedelta(hours=1)
        send_sample(uow, system_id, resumed)
        clock.current = resumed + timedelta(seconds=5)
        assert sweep(uow, clock).closed == [first]
        clock.current = resumed + timedelta(minutes=15)
        (second,) = sweep(uow, clock).opened
        assert second != first
        assert uow.events.get(second).gap_started_at == resumed


class TestInvariantsAndFailures:
    def test_inv12_gap_events_only_use_gap_states(self, uow, system_id):
        (event_id,) = sweep(uow, FixedClock(LAST_DATA + timedelta(hours=1))).opened
        assert uow.events.get(event_id).state in GAP_STATES

    @pytest.mark.parametrize("failing", ["ledger", "notifier"])
    def test_integration_failure_creates_nothing(self, uow, system_id, failing):
        ledger, notifier = RecordingLedger(), RecordingNotifier()
        (ledger if failing == "ledger" else notifier).fail_with = UnimplementedError(
            "x", "REQ-02", failing
        )
        result = sweep(uow, FixedClock(LAST_DATA + timedelta(hours=1)), ledger, notifier)
        assert result.opened == [] and uow.events.rows == {}

    def test_one_failing_system_does_not_block_the_others(self, uow, system_id):
        healthy = uow.seed_system(name="resume-ranker", last_seen_at=LAST_DATA)

        class FailOnce(RecordingNotifier):
            def enqueue(self, session, **kwargs):
                if not self.calls and not getattr(self, "failed", False):
                    self.failed = True
                    raise RuntimeError("boom")
                super().enqueue(session, **kwargs)

        notifier = FailOnce()
        result = sweep(uow, FixedClock(LAST_DATA + timedelta(hours=1)), notifier=notifier)
        assert len(result.opened) == 1
        assert {e.system_id for e in uow.events.rows.values()} <= {system_id, healthy}
        # The failed system is retried by the next sweep.
        retry = sweep(uow, FixedClock(LAST_DATA + timedelta(hours=1, seconds=5)), notifier=notifier)
        assert len(retry.opened) == 1
        assert {e.system_id for e in uow.events.rows.values()} == {system_id, healthy}
