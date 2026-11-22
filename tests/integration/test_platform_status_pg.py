"""Contract delta A1 and Δ5 on PostgreSQL: /healthz's reset_count survives resets and counts
them, and last_verification is the newest run of any trigger (D-29)."""

from datetime import timedelta

from app.api.health import platform_status
from app.core.config import Settings
from app.modules.ledger.checks import run_ledger_check
from app.modules.ledger.models import VerificationTrigger
from tests.conftest import T0
from tests.fakes import FixedClock


def run(make_uow, clock, trigger=VerificationTrigger.SCHEDULED):
    return run_ledger_check(
        make_uow(),
        clock=clock,
        interval=timedelta(days=1),
        max_delay=timedelta(minutes=15),
        force=True,
        trigger=trigger,
    )


def test_reset_count_survives_resets_and_counts_them(make_uow, pg_sessionmaker, monkeypatch):
    from app.modules.testing import service

    monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=True))
    with pg_sessionmaker() as s:
        assert platform_status(s, T0)["reset_count"] == 0
    service.reset_test_environment(make_uow())
    service.reset_test_environment(make_uow())
    with pg_sessionmaker() as s:
        assert platform_status(s, T0)["reset_count"] == 2


def test_last_verification_is_the_newest_run_of_any_trigger(make_uow, pg_sessionmaker):
    clock = FixedClock(T0)
    run(make_uow, clock)
    clock.advance(hours=1)
    run(make_uow, clock, VerificationTrigger.MANUAL)
    with pg_sessionmaker() as s:
        status = platform_status(s, T0 + timedelta(hours=1, seconds=90.7))
    assert status["last_verification"] == {
        "finished_at": "2026-09-27T21:00:00Z",
        "valid": True,
        "head_check": "NO_ANCHOR",
    }
    assert status["last_verification_age_seconds"] == 90  # whole seconds


def test_no_run_yet_and_age_never_negative(make_uow, pg_sessionmaker):
    with pg_sessionmaker() as s:
        assert platform_status(s, T0)["last_verification"] is None
        assert platform_status(s, T0)["last_verification_age_seconds"] is None
    run(make_uow, FixedClock(T0 + timedelta(days=30)))  # simulated clock ahead of real time
    with pg_sessionmaker() as s:
        assert platform_status(s, T0)["last_verification_age_seconds"] == 0


def test_reset_clears_runs_but_not_the_counter(make_uow, pg_sessionmaker, monkeypatch):
    from app.modules.testing import service

    monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=True))
    run(make_uow, FixedClock(T0))
    service.reset_test_environment(make_uow())
    with pg_sessionmaker() as s:
        status = platform_status(s, T0)
    assert status["last_verification"] is None and status["reset_count"] == 1
