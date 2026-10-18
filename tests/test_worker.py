"""The background worker's pass: gap sweep, notification delivery, REQ-04 overdue alerts."""

import logging
from datetime import UTC, datetime, timedelta

from app.modules.events.models import EventState, EventType
from app.modules.ledger.interface import LedgerEntryType
from app.modules.notifications.delivery import SendResult
from app.modules.worker.runner import Worker
from tests.fakes import ChainLedger, FixedClock, OutboxRecorder, RecordingSender

LAST_DATA = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)


def make_worker(uow, clock, *, sender=None, heartbeat=None, uow_factory=None):
    return Worker(
        uow_factory=uow_factory or (lambda: uow),
        ledger=ChainLedger(uow.ledger, clock),
        notifier=OutboxRecorder(uow.notifications, clock),
        sender=sender or RecordingSender(),
        clock_provider=lambda: clock,
        max_attempts=3,
        heartbeat=heartbeat,
    )


def test_ac02_gap_detected_and_reviewer_notified_in_one_pass(uow):
    """AC-02: withhold 3 intervals -> gap event, and the reviewer webhook is called right away."""
    uow.seed_system(last_seen_at=LAST_DATA)
    uow.seed_webhook()
    clock = FixedClock(LAST_DATA + timedelta(minutes=15))
    sender = RecordingSender()
    beats = []

    result = make_worker(uow, clock, sender=sender, heartbeat=lambda: beats.append(1)).run_once()

    (event_id,) = result.gaps.opened
    assert result.dispatch.delivered and result.errors == [] and beats == [1]
    event = uow.events.get(event_id)
    assert (event.type, event.state) == (EventType.MONITORING_GAP, EventState.REVIEWER_NOTIFIED)
    assert len(sender.calls) == 1
    assert [e.entry_type for e in uow.ledger.for_event(event_id)] == [
        LedgerEntryType.GAP_OPENED,
        LedgerEntryType.NOTIFIED,
    ]


def test_ac02_two_missed_intervals_do_nothing(uow):
    uow.seed_system(last_seen_at=LAST_DATA)
    uow.seed_webhook()
    sender = RecordingSender()
    result = make_worker(
        uow, FixedClock(LAST_DATA + timedelta(minutes=10)), sender=sender
    ).run_once()
    assert result.gaps.opened == [] and sender.calls == [] and uow.events.rows == {}


def test_a_failing_step_does_not_stop_the_others(uow, caplog):
    uow.seed_system(last_seen_at=LAST_DATA)
    calls = {"n": 0}

    def flaky_factory():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database blip")
        return uow

    beats = []
    worker = make_worker(
        uow,
        FixedClock(LAST_DATA + timedelta(hours=1)),
        uow_factory=flaky_factory,
        heartbeat=lambda: beats.append(1),
    )
    with caplog.at_level(logging.ERROR):
        result = worker.run_once()
    assert result.errors == ["gaps"] and result.dispatch is not None and beats == [1]
    assert "gap sweep failed" in caplog.text


def test_heartbeat_failure_is_reported(uow):
    def broken():
        raise RuntimeError("no table")

    result = make_worker(uow, FixedClock(LAST_DATA), heartbeat=broken).run_once()
    assert result.errors == ["heartbeat"]


def test_overdue_notifications_are_alerted_once(uow, caplog):
    """REQ-04: with no reachable webhook, the missed deadline is logged as an error, once."""
    uow.seed_system(last_seen_at=LAST_DATA)
    clock = FixedClock(LAST_DATA + timedelta(minutes=15))
    sender = RecordingSender(respond=lambda url: SendResult(False, 503, "HTTP 503"))
    worker = make_worker(uow, clock, sender=sender)
    assert worker.run_once().newly_overdue == 0  # gap opened, notification queued (no webhook)

    clock.advance(minutes=14)
    assert worker.run_once().newly_overdue == 0
    clock.advance(minutes=1, seconds=1)
    with caplog.at_level(logging.ERROR):
        assert worker.run_once().newly_overdue == 1
    assert "REQ-04" in caplog.text
    clock.advance(minutes=5)
    assert worker.run_once().newly_overdue == 0
