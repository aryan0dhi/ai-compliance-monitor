"""Ledger verification runs: GET and POST /v1/ledger/verifications (contract delta Δ5; REQ-06;
D-27, D-29)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.checks import run_ledger_check
from app.modules.ledger.models import HeadCheck
from app.modules.notifications.models import NotificationType
from tests.conftest import T0
from tests.fakes import ChainLedger, FixedClock, system_principal, user_principal

SYSTEM = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def chain(uow, clock):
    return ChainLedger(uow.ledger, clock)


@pytest.fixture
def api(uow, clock):
    state = {"principal": user_principal(Role.REVIEWER)}
    app = create_app()
    app.dependency_overrides.update(
        {
            get_principal: lambda: state["principal"],
            get_uow: lambda: uow,
            get_clock: lambda: clock,
        }
    )
    return TestClient(app), state


def append(chain, n=1):
    for i in range(n):
        chain.append(None, entry_type="VIEW", actor_id=None, payload={"n": i})


def scheduled(uow, clock):
    return run_ledger_check(
        uow, clock=clock, interval=timedelta(days=1), max_delay=timedelta(minutes=15)
    )


class TestRun:
    def test_a_reviewer_runs_one_now(self, api, uow, chain, clock):
        client, _ = api
        append(chain, 3)
        resp = client.post("/v1/ledger/verifications")
        assert resp.status_code == 201, resp.text
        run = resp.json()
        assert run == {
            "verification_id": run["verification_id"],
            "trigger": "MANUAL",
            "finished_at": T0.isoformat().replace("+00:00", "Z"),
            "valid": True,
            "entries_checked": 3,
            "first_mismatch": None,
            "head_seq": 3,
            "head_hash": uow.ledger.entries[-1].entry_hash,
            "head_check": "NO_ANCHOR",
            "problem": None,
        }
        (stored,) = uow.ledger_checks.rows
        assert str(stored.id) == run["verification_id"] and stored.trigger == "MANUAL"

    def test_a_manual_run_ignores_the_daily_interval(self, api, uow, chain, clock):
        client, _ = api
        append(chain)
        scheduled(uow, clock)
        assert client.post("/v1/ledger/verifications").status_code == 201
        assert [c.trigger for c in uow.ledger_checks.rows] == ["SCHEDULED", "MANUAL"]

    def test_head_check_compares_with_the_last_passing_run(self, api, uow, chain, clock):
        client, _ = api
        append(chain, 3)
        assert client.post("/v1/ledger/verifications").json()["head_check"] == "NO_ANCHOR"
        clock.advance(minutes=1)
        append(chain)
        assert client.post("/v1/ledger/verifications").json()["head_check"] == "MATCH"
        clock.advance(minutes=1)
        uow.ledger.entries[:] = uow.ledger.entries[:2]  # entries removed from the end
        run = client.post("/v1/ledger/verifications").json()
        assert (run["valid"], run["head_check"]) == (False, "MISMATCH")
        assert "removed from the end" in run["problem"]

    def test_a_failed_manual_run_alerts_like_a_scheduled_one(self, api, uow, chain, clock):
        client, _ = api
        append(chain, 2)
        client.post("/v1/ledger/verifications")
        uow.ledger.entries[0].payload = {"edited": True}
        clock.advance(minutes=1)
        run = client.post("/v1/ledger/verifications").json()
        assert run["valid"] is False and run["first_mismatch"] == 1
        alerts = [
            n
            for n in uow.notifications.rows.values()
            if n.notification_type == NotificationType.LEDGER_ALERT
        ]
        assert len(alerts) == 1

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
    def test_other_roles_may_not_run(self, api, uow, role):
        client, state = api
        state["principal"] = user_principal(role)
        assert client.post("/v1/ledger/verifications").status_code == 403
        assert uow.ledger_checks.rows == []

    def test_system_keys_may_not_run(self, api, uow):
        client, state = api
        state["principal"] = system_principal(__import__("uuid").UUID(SYSTEM))
        assert client.post("/v1/ledger/verifications").status_code == 403


class TestList:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER, Role.ADMIN])
    def test_readers_list_runs_newest_first(self, api, uow, chain, clock, role):
        client, state = api
        append(chain)
        scheduled(uow, clock)
        clock.advance(hours=1)
        client.post("/v1/ledger/verifications")
        state["principal"] = user_principal(role)
        runs = client.get("/v1/ledger/verifications").json()
        assert isinstance(runs, list)  # a bare array, like the audit trail
        assert [r["trigger"] for r in runs] == ["MANUAL", "SCHEDULED"]
        assert runs[0]["finished_at"] > runs[1]["finished_at"]

    def test_empty_before_any_run(self, api):
        client, state = api
        state["principal"] = user_principal(Role.VIEWER)
        assert client.get("/v1/ledger/verifications").json() == []

    def test_runs_from_before_migration_0016_show_no_head_check(self, api, uow, chain, clock):
        client, _ = api
        append(chain)
        scheduled(uow, clock)
        uow.ledger_checks.rows[0].head_check = None
        assert client.get("/v1/ledger/verifications").json()[0]["head_check"] is None

    def test_system_keys_may_not_list(self, api):
        client, state = api
        state["principal"] = system_principal(__import__("uuid").UUID(SYSTEM))
        assert client.get("/v1/ledger/verifications").status_code == 403


def test_scheduled_runs_are_marked_scheduled(uow, chain, clock):
    append(chain)
    result = scheduled(uow, clock)
    assert result is not None
    assert (result.check.trigger, result.check.head_check) == ("SCHEDULED", HeadCheck.NO_ANCHOR)


@pytest.mark.parametrize("method", ["get", "post"])
def test_without_a_key_it_is_401(method):
    """The delta's acceptance check: each new route answers 401 without a key."""
    resp = getattr(TestClient(create_app()), method)("/v1/ledger/verifications")
    assert resp.status_code == 401


def test_documented_in_openapi():
    path = create_app().openapi()["paths"]["/v1/ledger/verifications"]
    assert set(path) == {"get", "post"}
    assert "201" in path["post"]["responses"] and "403" in path["post"]["responses"]
