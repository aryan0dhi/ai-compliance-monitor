"""Article 73 deadline reminders (REQ-11, B-11, AC-12; D-24)."""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.modules.auth.interface import Role
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.dispatcher import delivery_body
from app.modules.review.disposition import (
    DispositionRequest,
    FieldConfirmation,
    record_disposition,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus
from app.modules.review.reminders import checkpoint, due_percents, sweep_reminders
from app.modules.worker.runner import Worker
from tests.conftest import T0
from tests.fakes import (
    ChainLedger,
    OutboxRecorder,
    RecordingNotifier,
    RecordingSender,
    system_principal,
    user_principal,
)

SYSTEM = UUID("550e8400-e29b-41d4-a716-446655440000")
ALL_NA = {name: FieldConfirmation(HarmFieldStatus.NOT_APPLICABLE) for name in HarmFieldName}
DAY = timedelta(days=1)
MAX_DELAY = timedelta(minutes=15)
OTHER, DEATH = DispositionTier.OTHER_SERIOUS, DispositionTier.DEATH
CRIT, NOT = DispositionTier.CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD, DispositionTier.NOT_SERIOUS


def test_checkpoints_split_the_window():
    """OTHER_SERIOUS: 15 days → 7.5 d, 12 d, 15 d after A."""
    deadline = T0 + 15 * DAY
    assert [checkpoint(T0, deadline, p) for p in (50, 80, 100)] == [
        T0 + timedelta(hours=180),
        T0 + 12 * DAY,
        deadline,
    ]


@pytest.mark.parametrize(
    ("percent", "due"),
    [
        (0, []),
        (49, []),
        (50, [50]),
        (79, [50]),
        (80, [50, 80]),
        (99, [50, 80]),
        (100, [50, 80, 100]),
    ],
)
def test_due_at_each_boundary(percent, due):
    """AC-12: one at 50 / 80 / 100 %, none at 49 / 79 / 99 % (and µs before still none)."""
    deadline = T0 + 15 * DAY
    now = checkpoint(T0, deadline, percent)
    assert due_percents(T0, deadline, now, set()) == due
    assert due_percents(T0, deadline, now - timedelta(microseconds=1), set()) == [
        p for p in due if p != percent
    ]


def test_sent_checkpoints_are_skipped():
    assert due_percents(T0, T0 + DAY, T0 + DAY, {50, 80}) == [100]


# --- the sweep on the in-memory unit of work ------------------------------------------------------


@pytest.fixture
def event_id(uow, ledger, clock) -> UUID:
    uow.seed_system(SYSTEM)
    uow.seed_threshold(
        SYSTEM,
        "accuracy",
        metric_type="performance",
        direction="below",
        limit="0.70",
        valid_from=T0 - DAY,
    )
    result = ingest_metric(
        MetricSubmission(
            system_id=SYSTEM,
            metric_name="accuracy",
            metric_type=MetricType.PERFORMANCE,
            value=Decimal("0.61"),
            sampled_at=T0,
        ),
        principal=system_principal(SYSTEM),
        uow=uow,
        ledger=ledger,
        notifier=RecordingNotifier(),
        clock=clock,
    )
    assert result.flagged_event_id is not None
    return result.flagged_event_id


def dispose(uow, ledger, clock, event_id, tier, rationale=None):
    record_disposition(
        event_id,
        DispositionRequest(tier=tier, rationale=rationale, harm_fields=ALL_NA),
        principal=user_principal(Role.REVIEWER),
        uow=uow,
        ledger=ledger,
        clock=clock,
    )


def sweep(uow, clock):
    return sweep_reminders(lambda: uow, clock=clock, max_delay=MAX_DELAY).queued


def queued(uow):
    rows = sorted(uow.notifications.rows.values(), key=lambda n: n.reminder_percent or 0)
    return [(n.reminder_percent, n.reminder_deadline_utc) for n in rows if n.reminder_percent]


class TestSweep:
    def test_one_reminder_per_checkpoint_as_time_passes(self, uow, ledger, clock, event_id):
        dispose(uow, ledger, clock, event_id, OTHER)
        a = clock.now()
        deadline = a + 15 * DAY
        for at, expected in [
            (a + 7 * DAY, []),  # 46 %
            (checkpoint(a, deadline, 50) - timedelta(seconds=1), []),
            (checkpoint(a, deadline, 50), [50]),
            (checkpoint(a, deadline, 50), []),  # the same pass again: nothing new
            (checkpoint(a, deadline, 79), []),
            (checkpoint(a, deadline, 80), [80]),
            (checkpoint(a, deadline, 99), []),
            (deadline, [100]),
            (deadline + 30 * DAY, []),
        ]:
            clock.current = at
            assert sweep(uow, clock) == [(event_id, p) for p in expected], at
        assert queued(uow) == [(50, deadline), (80, deadline), (100, deadline)]
        reminder = next(n for n in uow.notifications.rows.values() if n.reminder_percent == 80)
        assert reminder.notification_type == "reminder"
        assert reminder.created_at == checkpoint(a, deadline, 80)
        assert reminder.deliver_by == reminder.created_at + MAX_DELAY
        assert reminder.payload["deadline_utc"] == deadline.isoformat()
        assert reminder.payload["state"] == "DISPOSITIONED"

    def test_a_late_pass_catches_up_in_order(self, uow, ledger, clock, event_id):
        """Model 5 Ex-1 jumps straight to the deadline: all three go out."""
        dispose(uow, ledger, clock, event_id, DEATH)
        clock.advance(days=11)
        assert [p for _, p in sweep(uow, clock)] == [50, 80, 100]

    def test_no_reminders_before_a_disposition_or_for_not_serious(
        self, uow, ledger, clock, event_id
    ):
        clock.advance(days=30)
        assert sweep(uow, clock) == []  # FLAGGED
        dispose(uow, ledger, clock, event_id, NOT, rationale="no harm")
        clock.advance(days=30)
        assert sweep(uow, clock) == []  # CLOSED

    def test_closing_stops_the_rest(self, uow, ledger, clock, event_id):
        dispose(uow, ledger, clock, event_id, OTHER)
        clock.advance(days=8)
        assert [p for _, p in sweep(uow, clock)] == [50]
        dispose(uow, ledger, clock, event_id, NOT, rationale="turned out harmless")
        clock.advance(days=30)
        assert sweep(uow, clock) == []

    def test_revision_moves_the_checkpoints_still_to_come(self, uow, ledger, clock, event_id):
        """OTHER_SERIOUS at A (window 15 d); DEATH at A+3d → window A..A+10d, 50 % at A+5d."""
        dispose(uow, ledger, clock, event_id, OTHER)
        a = clock.now()
        clock.advance(days=3)
        dispose(uow, ledger, clock, event_id, DEATH)
        clock.current = a + 5 * DAY
        assert [p for _, p in sweep(uow, clock)] == [50]
        assert queued(uow) == [(50, a + 10 * DAY)]

    def test_sent_checkpoints_are_not_sent_again_after_a_revision(
        self, uow, ledger, clock, event_id
    ):
        """50 % went out on the 15-day window; a stricter tier makes the new deadline already
        past, so 80 and 100 go out at once and 50 is not repeated."""
        dispose(uow, ledger, clock, event_id, OTHER)
        a = clock.now()
        clock.advance(days=8)
        assert [p for _, p in sweep(uow, clock)] == [50]
        dispose(uow, ledger, clock, event_id, CRIT)  # deadline min(A+15d, A+2d) = A+2d
        assert [p for _, p in sweep(uow, clock)] == [80, 100]
        assert queued(uow) == [(50, a + 15 * DAY), (80, a + 2 * DAY), (100, a + 2 * DAY)]

    def test_looser_revision_keeps_what_was_sent(self, uow, ledger, clock, event_id):
        dispose(uow, ledger, clock, event_id, DEATH)
        a = clock.now()
        clock.advance(days=6)
        assert [p for _, p in sweep(uow, clock)] == [50]  # 60 % of 10 days
        dispose(uow, ledger, clock, event_id, OTHER, rationale="no fatality")  # A + 15 d
        assert sweep(uow, clock) == []  # 40 % of the new window, 50 already sent
        clock.current = a + 12 * DAY
        assert [p for _, p in sweep(uow, clock)] == [80]


def test_delivery_body_carries_the_reminder(uow, ledger, clock, event_id):
    dispose(uow, ledger, clock, event_id, DEATH)
    deadline = clock.now() + 10 * DAY
    clock.advance(days=5)
    sweep(uow, clock)
    (reminder,) = [n for n in uow.notifications.rows.values() if n.reminder_percent]
    body = json.loads(delivery_body(reminder))
    assert body["notification_type"] == "reminder"
    assert body["reminder"] == {"elapsed_percent": 50, "deadline_utc": deadline.isoformat()}
    assert {"event_id", "type", "system_id", "detected_at"} <= set(body["event"])
    reminder.reminder_percent = None
    reminder.reminder_deadline_utc = None
    assert "reminder" not in json.loads(delivery_body(reminder))


def test_worker_delivers_each_checkpoint_once_to_reminder_webhooks(uow, ledger, clock, event_id):
    """AC-12 end to end on the doubles: many worker passes, two webhooks for `reminder`, one for
    flag notifications only. Each reminder webhook gets 50, 80 and 100 exactly once."""
    hooks = [
        uow.seed_webhook("https://a.example/r", event_types=("reminder",)),
        uow.seed_webhook("https://b.example/r", event_types=("flag_notification", "reminder")),
    ]
    flags_only = uow.seed_webhook("https://c.example/f", event_types=("flag_notification",))
    dispose(uow, ledger, clock, event_id, DEATH)
    sender = RecordingSender()
    worker = Worker(
        uow_factory=lambda: uow,
        ledger=ChainLedger(uow.ledger, clock),
        notifier=OutboxRecorder(uow.notifications, clock),
        sender=sender,
        clock_provider=lambda: clock,
        max_attempts=3,
    )
    for _ in range(24):  # ten and a half days in 12-hour passes
        clock.advance(hours=11)
        assert worker.run_once().errors == []
    reminders = [
        (c["url"], json.loads(c["body"])["reminder"]["elapsed_percent"])
        for c in sender.calls
        if json.loads(c["body"])["notification_type"] == "reminder"
    ]
    assert sorted(reminders) == sorted((h.url, p) for h in hooks for p in (50, 80, 100))
    assert all(url != flags_only.url for url, _ in reminders)
