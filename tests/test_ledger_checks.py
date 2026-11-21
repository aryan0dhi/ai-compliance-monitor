"""Scheduled ledger verification and the critical alert (REQ-06, Model 5 Ex-5, #71; D-27)."""

import json
import logging
from datetime import timedelta

import pytest

from app.modules.ledger.checks import run_ledger_check
from app.modules.ledger.interface import LedgerEntryType
from app.modules.notifications.dispatcher import delivery_body, dispatch_due
from app.modules.notifications.models import NotificationType
from app.modules.worker.runner import Worker
from tests.conftest import T0
from tests.fakes import ChainLedger, FixedClock, OutboxRecorder, RecordingSender

DAY = timedelta(days=1)
MAX_DELAY = timedelta(minutes=15)


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def chain(uow, clock):
    return ChainLedger(uow.ledger, clock)


def append(chain, n=1):
    for i in range(n):
        chain.append(None, entry_type="VIEW", actor_id=None, payload={"n": i})


def check(uow, clock, **kwargs):
    return run_ledger_check(uow, clock=clock, interval=DAY, max_delay=MAX_DELAY, **kwargs)


def alerts(uow):
    return [
        n
        for n in uow.notifications.rows.values()
        if n.notification_type == NotificationType.LEDGER_ALERT
    ]


def tamper(uow, seq, **changes):
    """Edit a stored entry behind the writer's back, as a DBA could."""
    entry = next(e for e in uow.ledger.entries if e.seq == seq)
    for key, value in changes.items():
        setattr(entry, key, value)


class TestSchedule:
    def test_first_check_runs_at_once_and_passes(self, uow, clock, chain):
        append(chain, 3)
        result = check(uow, clock)
        assert result is not None and not result.alert_queued
        stored = result.check
        assert (stored.valid, stored.entries_checked, stored.head_seq) == (True, 3, 3)
        assert stored.head_hash == uow.ledger.entries[-1].entry_hash
        assert (stored.problem, stored.first_mismatch, stored.alerted) == (None, None, False)
        assert uow.ledger_checks.rows == [stored]
        assert alerts(uow) == []

    def test_runs_once_per_interval_of_service_clock(self, uow, clock, chain):
        check(uow, clock)
        clock.advance(hours=23, minutes=59)
        assert check(uow, clock) is None
        clock.advance(minutes=1)
        assert check(uow, clock) is not None
        assert len(uow.ledger_checks.rows) == 2

    def test_force_runs_whenever_asked(self, uow, clock):
        check(uow, clock)
        assert check(uow, clock, force=True) is not None

    def test_empty_ledger_passes(self, uow, clock):
        stored = check(uow, clock).check
        assert (stored.valid, stored.head_seq, stored.head_hash) == (True, 0, "0" * 64)

    def test_checking_appends_nothing_to_the_ledger(self, uow, clock, chain):
        append(chain, 2)
        check(uow, clock)
        assert len(uow.ledger.entries) == 2


class TestDetection:
    @pytest.mark.parametrize(
        "changes",
        [{"payload": {"edited": True}}, {"actor_id": None, "entry_type": "FLAGGED"}],
    )
    def test_an_edited_entry_alerts(self, uow, clock, chain, changes, caplog):
        """Ex-5: a row edited behind the app's back is found by the next scheduled check."""
        append(chain, 3)
        check(uow, clock)
        tamper(uow, 2, **changes)
        clock.advance(days=1)
        with caplog.at_level(logging.CRITICAL):
            result = check(uow, clock)

        stored = result.check
        assert (stored.valid, stored.first_mismatch, stored.alerted) == (False, 2, True)
        assert "broken at entry 2" in stored.problem
        assert result.alert_queued
        (alert,) = alerts(uow)
        assert alert.event_id is None
        assert alert.deliver_by == clock.now() + MAX_DELAY
        assert alert.payload["severity"] == "critical"
        assert alert.payload["check_id"] == str(stored.id)
        assert alert.payload["first_mismatch"] == 2
        assert alert.payload["last_passing_head"]["seq"] == 3
        assert "ledger check failed" in caplog.text

    def test_entries_removed_from_the_end_alert(self, uow, clock, chain):
        """The chain alone can't show truncation (D-14); the last passing head can."""
        append(chain, 4)
        check(uow, clock)
        uow.ledger.entries = uow.ledger.entries[:2]
        clock.advance(days=1)
        stored = check(uow, clock).check
        assert not stored.valid and stored.first_mismatch is None
        assert "removed from the end" in stored.problem
        assert len(alerts(uow)) == 1

    def test_a_replaced_end_alerts(self, uow, clock, chain):
        """Remove the last entry and append a different one: same length, different hash."""
        append(chain, 3)
        check(uow, clock)
        uow.ledger.entries = uow.ledger.entries[:2]
        chain.append(None, entry_type="VIEW", actor_id=None, payload={"forged": True})
        clock.advance(days=1)
        stored = check(uow, clock).check
        assert not stored.valid and "no longer has the hash" in stored.problem

    def test_growth_after_a_passing_check_is_fine(self, uow, clock, chain):
        append(chain, 2)
        check(uow, clock)
        append(chain, 5)
        clock.advance(days=1)
        stored = check(uow, clock).check
        assert stored.valid and stored.head_seq == 7

    def test_emptied_ledger_alerts(self, uow, clock, chain):
        append(chain, 2)
        check(uow, clock)
        uow.ledger.entries = []
        clock.advance(days=1)
        assert not check(uow, clock).check.valid


class TestIncidents:
    def test_one_alert_per_incident(self, uow, clock, chain):
        append(chain, 3)
        check(uow, clock)
        tamper(uow, 2, payload={"edited": True})
        for _ in range(3):
            clock.advance(days=1)
            check(uow, clock)
        assert [c.alerted for c in uow.ledger_checks.rows] == [False, True, False, False]
        assert len(alerts(uow)) == 1

    def test_a_new_incident_after_recovery_alerts_again(self, uow, clock, chain):
        append(chain, 3)
        check(uow, clock)
        original = uow.ledger.entries[1].payload
        tamper(uow, 2, payload={"edited": True})
        clock.advance(days=1)
        check(uow, clock)
        tamper(uow, 2, payload=original)  # restored from a backup
        clock.advance(days=1)
        assert check(uow, clock).check.valid
        tamper(uow, 3, payload={"again": True})
        clock.advance(days=1)
        assert check(uow, clock).alert_queued
        assert len(alerts(uow)) == 2

    def test_a_broken_first_check_alerts(self, uow, clock, chain):
        append(chain, 2)
        tamper(uow, 1, payload={"edited": True})
        result = check(uow, clock)
        assert result.alert_queued and result.check.first_mismatch == 1
        assert alerts(uow)[0].payload["last_passing_head"] is None


class TestDelivery:
    def test_alert_reaches_subscribed_webhooks_only(self, uow, clock, chain):
        append(chain, 2)
        check(uow, clock)
        tamper(uow, 1, payload={"edited": True})
        admin = uow.seed_webhook("https://ops.example.com/alerts", event_types=("ledger_alert",))
        uow.seed_webhook("https://hooks.example.com/reviewer")  # flag_notification, reminder
        clock.advance(days=1)
        check(uow, clock)
        sender = RecordingSender()
        result = dispatch_due(lambda: uow, sender=sender, ledger=chain, clock=clock, max_attempts=3)

        (call,) = sender.calls
        assert call["url"] == admin.url
        body = json.loads(call["body"])
        assert body["notification_type"] == "ledger_alert"
        assert "event_id" not in body and "event" not in body
        assert body["alert"]["severity"] == "critical"
        assert len(result.delivered) == 1
        notified = [e for e in uow.ledger.entries if e.entry_type == LedgerEntryType.NOTIFIED]
        assert len(notified) == 1 and notified[0].event_id is None

    def test_delivery_body_is_stable(self, uow, clock, chain):
        append(chain, 1)
        tamper(uow, 1, payload={"x": 1})
        check(uow, clock)
        (alert,) = alerts(uow)
        assert delivery_body(alert) == delivery_body(alert)
        assert set(json.loads(delivery_body(alert))) == {
            "notification_id",
            "notification_type",
            "created_at",
            "deliver_by",
            "alert",
        }


class TestWorker:
    def make_worker(self, uow, clock, sender):
        return Worker(
            uow_factory=lambda: uow,
            ledger=ChainLedger(uow.ledger, clock),
            notifier=OutboxRecorder(uow.notifications, clock),
            sender=sender,
            clock_provider=lambda: clock,
            max_attempts=3,
            ledger_check_interval=DAY,
        )

    def test_worker_checks_daily_and_alerts_in_the_same_pass(self, uow, clock, chain):
        append(chain, 3)
        uow.seed_webhook("https://ops.example.com/alerts", event_types=("ledger_alert",))
        sender = RecordingSender()
        worker = self.make_worker(uow, clock, sender)

        first = worker.run_once()
        assert first.ledger_check is not None and first.ledger_check.check.valid
        clock.advance(hours=1)
        assert worker.run_once().ledger_check is None  # not due yet

        tamper(uow, 2, payload={"edited": True})
        clock.advance(days=1)
        result = worker.run_once()
        assert result.ledger_check.alert_queued and result.errors == []
        assert [json.loads(c["body"])["notification_type"] for c in sender.calls] == [
            "ledger_alert"
        ]

    def test_a_failing_check_does_not_stop_the_pass(self, uow, clock, monkeypatch):
        def broken(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.modules.worker.runner.run_ledger_check", broken)
        result = self.make_worker(uow, clock, RecordingSender()).run_once()
        assert result.errors == ["ledger_check"] and result.dispatch is not None
