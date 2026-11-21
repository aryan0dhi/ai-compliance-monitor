"""Scheduled ledger verification on PostgreSQL: direct SQL tampering found by the worker's check,
the alert in the outbox, and the new constraints (REQ-06, Model 5 Ex-5, #71; D-27)."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.modules.ledger.checks import run_ledger_check
from app.modules.ledger.models import LedgerCheck
from app.modules.ledger.writer import SqlLedgerWriter
from app.modules.notifications.dispatcher import dispatch_due
from app.modules.notifications.models import Notification, NotificationType, Webhook
from tests.conftest import T0
from tests.fakes import FixedClock, RecordingSender

DAY = timedelta(days=1)


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def ledger(clock):
    return SqlLedgerWriter(lambda: clock)


def append(make_uow, ledger, n):
    for i in range(n):
        with make_uow() as uow:
            ledger.append(uow.session, entry_type="VIEW", actor_id=None, payload={"n": i})


def check(make_uow, clock):
    return run_ledger_check(make_uow(), clock=clock, interval=DAY, max_delay=timedelta(minutes=15))


def checks(sessionmaker) -> list[LedgerCheck]:
    with sessionmaker() as s:
        return list(s.scalars(select(LedgerCheck).order_by(LedgerCheck.checked_at)))


def alerts(sessionmaker) -> list[Notification]:
    with sessionmaker() as s:
        return list(
            s.scalars(
                select(Notification).where(
                    Notification.notification_type == NotificationType.LEDGER_ALERT
                )
            )
        )


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        (
            "UPDATE ledger_entry SET payload = '{\"edited\": true}' WHERE seq = 2",
            "broken at entry 2",
        ),
        ("DELETE FROM ledger_entry WHERE seq = 2", "broken at entry 3"),
        ("DELETE FROM ledger_entry WHERE seq >= 3", "removed from the end"),
    ],
)
def test_ex5_tampering_is_found_by_the_next_check_and_alerted_once(
    make_uow, pg_sessionmaker, pg_engine, ledger, clock, tamper, problem
):
    append(make_uow, ledger, 4)
    assert check(make_uow, clock).check.valid
    with pg_engine.begin() as conn:
        conn.execute(text(tamper))

    clock.advance(days=1)
    result = check(make_uow, clock)
    assert result.alert_queued and problem in result.check.problem
    clock.advance(days=1)
    assert not check(make_uow, clock).alert_queued  # same incident

    stored = checks(pg_sessionmaker)
    assert [(c.valid, c.alerted) for c in stored] == [(True, False), (False, True), (False, False)]
    (alert,) = alerts(pg_sessionmaker)
    assert alert.event_id is None and alert.payload["severity"] == "critical"


def test_alert_is_delivered_to_an_admin_webhook(
    make_uow, pg_sessionmaker, pg_engine, ledger, clock
):
    append(make_uow, ledger, 2)
    check(make_uow, clock)
    with pg_sessionmaker.begin() as s:
        s.add(
            Webhook(
                id=uuid.uuid4(),
                url="https://ops.example.com/alerts",
                event_types=["ledger_alert"],
                secret="s",
                created_by=None,
                created_at=T0,
                disabled_at=None,
            )
        )
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE ledger_entry SET payload = '{}' WHERE seq = 1"))
    clock.advance(days=1)
    check(make_uow, clock)

    sender = RecordingSender()
    result = dispatch_due(make_uow, sender=sender, ledger=ledger, clock=clock, max_attempts=3)
    assert len(result.delivered) == 1 and len(sender.calls) == 1
    with pg_sessionmaker() as s:
        notified = s.execute(
            text("SELECT event_id FROM ledger_entry WHERE entry_type = 'NOTIFIED'")
        ).all()
    assert notified == [(None,)]


def test_check_rows_are_constrained(pg_engine):
    good = {
        "id": uuid.uuid4(),
        "t": T0,
        "v": True,
        "n": 0,
        "s": 0,
        "h": "0" * 64,
        "m": None,
        "p": None,
        "a": False,
    }
    sql = text(
        "INSERT INTO ledger_check (id, checked_at, valid, entries_checked, head_seq, head_hash,"
        " first_mismatch, problem, alerted) VALUES (:id, :t, :v, :n, :s, :h, :m, :p, :a)"
    )
    with pg_engine.begin() as conn:
        conn.execute(sql, good)
    for bad in (
        {"v": False},  # a failure names its problem
        {"p": "broken"},  # ... and a pass has none
        {"a": True},  # only failures alert
        {"h": "xyz"},
        {"s": -1},
    ):
        with pytest.raises(IntegrityError), pg_engine.begin() as conn:
            conn.execute(sql, {**good, "id": uuid.uuid4(), **bad})


def test_only_ledger_alerts_have_no_event(pg_engine):
    sql = text(
        "INSERT INTO notification (id, event_id, notification_type, payload, created_at,"
        " deliver_by) VALUES (:id, NULL, :t, '{}', :c, :c)"
    )
    with pg_engine.begin() as conn:
        conn.execute(sql, {"id": uuid.uuid4(), "t": "ledger_alert", "c": T0})
    with pytest.raises(IntegrityError), pg_engine.begin() as conn:
        conn.execute(sql, {"id": uuid.uuid4(), "t": "flag_notification", "c": T0})
