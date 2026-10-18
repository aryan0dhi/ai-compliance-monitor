"""Reviewer notifications: outbox (REQ-04, Q7.2) and webhook dispatch (AC-04, AC-02 delivery)."""

import json
import logging
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.modules.events.models import EventState
from app.modules.ledger.interface import LedgerEntryType
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.delivery import SendResult
from app.modules.notifications.dispatcher import (
    claim_due,
    delivery_body,
    dispatch_due,
    record_result,
    retry_delay,
)
from app.modules.notifications.models import DeliveryStatus, NotificationType
from app.modules.notifications.outbox import OutboxNotifier, build_notification
from tests.conftest import T0
from tests.fakes import (
    ChainLedger,
    FixedClock,
    OutboxRecorder,
    RecordingSender,
    system_principal,
)
from tests.test_event_queries import make_event

MAX_ATTEMPTS = 3


def fail(status=503):
    return lambda url: SendResult(ok=False, status_code=status, error=f"HTTP {status}")


@pytest.fixture
def chain(uow, clock):
    return ChainLedger(uow.ledger, clock)


@pytest.fixture
def event(uow):
    e = make_event(uow.seed_system())
    uow.events.rows[e.id] = e
    return e


def enqueue(uow, clock, event, *, max_delay=timedelta(minutes=15), type_="flag_notification"):
    OutboxRecorder(uow.notifications, clock, max_delay).enqueue(
        None, notification_type=type_, event_id=event.id, payload={"event_id": str(event.id)}
    )
    (n,) = [n for n in uow.notifications.rows.values() if n.event_id == event.id][-1:]
    return n


def dispatch(uow, clock, sender, ledger):
    return dispatch_due(
        lambda: uow, sender=sender, ledger=ledger, clock=clock, max_attempts=MAX_ATTEMPTS
    )


class TestOutbox:
    def test_deliver_by_is_detection_plus_default_15_minutes(self, clock):
        n = build_notification(
            event_id=uuid4(),
            notification_type="flag_notification",
            payload={},
            now=clock.now(),
            max_delay=timedelta(seconds=Settings().notify_max_delay_s),
        )
        assert n.deliver_by - n.created_at == timedelta(minutes=15)
        assert n.created_at == clock.now() and n.dispatched_at is None

    def test_ac04_alternate_configured_delay_is_honoured(self, clock, monkeypatch):
        from app.modules.notifications import outbox

        monkeypatch.setattr(outbox, "get_settings", lambda: Settings(notify_max_delay_s=300))
        assert outbox.notify_max_delay() == timedelta(minutes=5)

    def test_rejects_unknown_type_and_float_payloads(self, clock):
        with pytest.raises(ValueError):
            build_notification(
                event_id=uuid4(),
                notification_type="sms",
                payload={},
                now=clock.now(),
                max_delay=timedelta(minutes=15),
            )
        with pytest.raises(TypeError):
            build_notification(
                event_id=uuid4(),
                notification_type="reminder",
                payload={"v": 0.3},
                now=clock.now(),
                max_delay=timedelta(minutes=15),
            )

    def test_outbox_notifier_reads_clock_and_delay_per_call(self, clock):
        notifier = OutboxNotifier(lambda: clock, lambda: timedelta(minutes=5))
        with pytest.raises(RuntimeError, match="session"):
            notifier.enqueue(None, notification_type="reminder", event_id=uuid4(), payload={})

    def test_settings_reject_a_poll_interval_slower_than_the_deadline(self):
        with pytest.raises(ValueError, match="WORKER_POLL_INTERVAL_S"):
            Settings(notify_max_delay_s=10, worker_poll_interval_s=10)


class TestDispatch:
    def test_delivers_and_records_a_chained_notified_entry(self, uow, clock, chain, event):
        hook = uow.seed_webhook()
        n = enqueue(uow, clock, event)
        sender = RecordingSender()
        clock.advance(seconds=5)

        result = dispatch(uow, clock, sender, chain)

        assert result.delivered == [n.id] and result.fanned_out == 1
        (call,) = sender.calls
        assert call["url"] == hook.url and call["secret"] == hook.secret
        body = json.loads(call["body"])
        assert body["notification_id"] == str(n.id) and body["event_id"] == str(event.id)
        assert body["notification_type"] == "flag_notification"
        assert body["event"] == n.payload
        delivery = uow.notifications.get_delivery(n.id, hook.id)
        assert delivery.status == DeliveryStatus.DELIVERED and delivery.attempts == 1
        assert delivery.delivered_at == clock.now()
        (entry,) = uow.ledger.entries
        assert entry.entry_type == LedgerEntryType.NOTIFIED and entry.event_id == event.id
        assert entry.payload["within_max_delay"] is True
        assert entry.payload["delivered_at"] == clock.now().isoformat()

    def test_nothing_is_sent_twice(self, uow, clock, chain, event):
        uow.seed_webhook()
        enqueue(uow, clock, event)
        sender = RecordingSender()
        dispatch(uow, clock, sender, chain)
        clock.advance(minutes=10)
        assert dispatch(uow, clock, sender, chain).delivered == []
        assert len(sender.calls) == 1

    def test_only_subscribed_webhooks_receive_a_type(self, uow, clock, chain, event):
        reminders = uow.seed_webhook("https://a.example/r", event_types=("reminder",))
        flags = uow.seed_webhook("https://b.example/f", event_types=("flag_notification",))
        enqueue(uow, clock, event)
        sender = RecordingSender()
        dispatch(uow, clock, sender, chain)
        assert [c["url"] for c in sender.calls] == [flags.url]
        assert reminders.id not in {wid for _, wid in uow.notifications.deliveries}

    def test_waits_for_a_webhook_then_delivers(self, uow, clock, chain, event):
        n = enqueue(uow, clock, event)
        sender = RecordingSender()
        assert dispatch(uow, clock, sender, chain).fanned_out == 0
        assert n.dispatched_at is None and sender.calls == []
        uow.seed_webhook()
        clock.advance(seconds=5)
        assert dispatch(uow, clock, sender, chain).delivered == [n.id]

    def test_each_webhook_is_delivered_independently(self, uow, clock, chain, event):
        good = uow.seed_webhook("https://good.example/")
        bad = uow.seed_webhook("https://bad.example/")
        n = enqueue(uow, clock, event)
        sender = RecordingSender(
            respond=lambda url: fail()(url) if "bad" in url else SendResult(True, 204, None)
        )
        dispatch(uow, clock, sender, chain)
        assert uow.notifications.get_delivery(n.id, good.id).status == DeliveryStatus.DELIVERED
        bad_delivery = uow.notifications.get_delivery(n.id, bad.id)
        assert bad_delivery.status == DeliveryStatus.PENDING
        assert bad_delivery.last_status_code == 503 and bad_delivery.last_error == "HTTP 503"

    def test_retries_with_backoff_then_gives_up(self, uow, clock, chain, event, caplog):
        hook = uow.seed_webhook()
        n = enqueue(uow, clock, event)
        sender = RecordingSender(respond=fail())

        dispatch(uow, clock, sender, chain)  # attempt 1
        delivery = uow.notifications.get_delivery(n.id, hook.id)
        assert delivery.next_attempt_at == clock.now() + timedelta(seconds=10)
        clock.advance(seconds=9)
        dispatch(uow, clock, sender, chain)  # not due yet
        assert len(sender.calls) == 1
        clock.advance(seconds=1)
        dispatch(uow, clock, sender, chain)  # attempt 2
        assert delivery.next_attempt_at == clock.now() + timedelta(seconds=20)
        clock.advance(seconds=20)
        with caplog.at_level(logging.ERROR):
            result = dispatch(uow, clock, sender, chain)  # attempt 3 = MAX_ATTEMPTS
        assert result.gave_up == 1 and delivery.status == DeliveryStatus.FAILED
        assert "abandoned after 3 attempts" in caplog.text
        clock.advance(hours=1)
        dispatch(uow, clock, sender, chain)
        assert len(sender.calls) == 3 and uow.ledger.entries == []

    def test_retry_delay_schedule(self):
        assert [retry_delay(n).total_seconds() for n in range(1, 8)] == [
            10,
            20,
            40,
            80,
            160,
            300,
            300,
        ]

    def test_late_delivery_is_flagged_in_ledger_and_logs(self, uow, clock, chain, event, caplog):
        uow.seed_webhook()
        enqueue(uow, clock, event, max_delay=timedelta(minutes=5))
        clock.advance(minutes=5, seconds=1)
        with caplog.at_level(logging.ERROR):
            dispatch(uow, clock, RecordingSender(), chain)
        assert uow.ledger.entries[0].payload["within_max_delay"] is False
        assert "REQ-04" in caplog.text

    def test_delivery_removed_mid_flight_is_ignored(self, uow, clock, chain, event):
        hook = uow.seed_webhook()
        enqueue(uow, clock, event)
        dispatch(uow, clock, RecordingSender(respond=fail()), chain)
        (job,) = claim_due(uow, now=clock.now() + timedelta(minutes=1))
        uow.notifications.deliveries.clear()  # e.g. /v1/test/reset
        status = record_result(
            uow,
            job,
            ok=True,
            status_code=200,
            error=None,
            now=clock.now(),
            ledger=chain,
            max_attempts=MAX_ATTEMPTS,
        )
        assert status is None and uow.ledger.entries == [] and hook

    def test_count_undelivered_due_between(self, uow, clock, chain, event):
        uow.seed_webhook()
        delivered = enqueue(uow, clock, event)
        dispatch(uow, clock, RecordingSender(), chain)
        other = make_event(event.system_id, minutes=1)
        uow.events.rows[other.id] = other
        stuck = enqueue(uow, clock, other)
        dispatch(uow, clock, RecordingSender(respond=fail()), chain)
        deadline = stuck.deliver_by
        repo = uow.notifications
        assert delivered.deliver_by == deadline
        assert repo.count_undelivered_due_between(deadline - timedelta(seconds=1), deadline) == 1
        assert repo.count_undelivered_due_between(deadline, deadline + timedelta(hours=1)) == 0

    def test_body_is_stable_json(self, uow, clock, event):
        n = enqueue(uow, clock, event)
        assert delivery_body(n) == delivery_body(n)
        assert json.loads(delivery_body(n))["deliver_by"] == n.deliver_by.isoformat()


class TestGapNotification:
    @pytest.fixture
    def gap(self, uow):
        e = make_event(uow.seed_system(), type_="monitoring_gap", state="GAP_OPEN")
        e.gap_started_at = e.detected_at - timedelta(minutes=15)
        uow.events.rows[e.id] = e
        return e

    def test_delivery_moves_gap_to_reviewer_notified(self, uow, clock, chain, gap):
        uow.seed_webhook()
        enqueue(uow, clock, gap)
        dispatch(uow, clock, RecordingSender(), chain)
        assert gap.state == EventState.REVIEWER_NOTIFIED
        assert uow.ledger.entries[0].payload["state"] == "REVIEWER_NOTIFIED"

    def test_closed_gap_keeps_its_state(self, uow, clock, chain, gap):
        uow.seed_webhook()
        enqueue(uow, clock, gap)
        gap.state, gap.gap_ended_at = EventState.GAP_CLOSED.value, clock.now()
        dispatch(uow, clock, RecordingSender(), chain)
        assert gap.state == EventState.GAP_CLOSED
        assert "state" not in uow.ledger.entries[0].payload

    def test_reminders_do_not_change_gap_state(self, uow, clock, chain, gap):
        uow.seed_webhook()
        enqueue(uow, clock, gap, type_=NotificationType.REMINDER)
        dispatch(uow, clock, RecordingSender(), chain)
        assert gap.state == EventState.GAP_OPEN


@pytest.mark.parametrize("max_delay", [timedelta(minutes=15), timedelta(minutes=5)])
def test_ac04_breach_to_webhook_within_configured_delay(uow, max_delay):
    """AC-04: flag at detection, deliver on the next worker pass, within the configured delay."""
    clock = FixedClock(T0)
    system_id = uow.seed_system()
    uow.seed_threshold(
        system_id,
        "drift_score",
        metric_type="drift",
        direction="above",
        limit="0.30",
        valid_from=T0 - timedelta(days=1),
    )
    uow.seed_webhook()
    chain = ChainLedger(uow.ledger, clock)
    result = ingest_metric(
        MetricSubmission(
            system_id=system_id,
            metric_name="drift_score",
            metric_type=MetricType.DRIFT,
            value=Decimal("0.35"),
            sampled_at=T0,
        ),
        principal=system_principal(system_id),
        uow=uow,
        ledger=chain,
        notifier=OutboxRecorder(uow.notifications, clock, max_delay),
        clock=clock,
    )
    detected_at = uow.events.get(result.flagged_event_id).detected_at
    clock.advance(seconds=5)  # one worker poll interval
    sender = RecordingSender()
    dispatch(uow, clock, sender, chain)

    (notified,) = [e for e in uow.ledger.entries if e.entry_type == LedgerEntryType.NOTIFIED]
    delivered_at = notified.at
    assert delivered_at - detected_at <= max_delay
    assert notified.payload["deliver_by"] == (detected_at + max_delay).isoformat()
    assert json.loads(sender.calls[0]["body"])["event"]["event_id"] == str(result.flagged_event_id)
    # The audit chain for the event: FLAGGED then NOTIFIED.
    assert [e.entry_type for e in uow.ledger.for_event(result.flagged_event_id)] == [
        "FLAGGED",
        "NOTIFIED",
    ]
