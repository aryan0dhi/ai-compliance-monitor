"""/v1/test/* (Section 5.1): only in the test environment, admin only, 404 in production."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import AppEnv, Settings
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.testing import router as testing_router
from app.modules.testing.clock import get_simulated_clock
from app.modules.testing.service import ResetNotAllowed, reset_test_environment
from tests.fakes import FakeUnitOfWork, user_principal

REAL_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


class FakeSimulatedClock:
    def __init__(self) -> None:
        self.offset = 0
        self.invalidated = 0

    def now(self) -> datetime:
        return REAL_NOW + timedelta(seconds=self.offset)

    def offset_seconds(self) -> int:
        return self.offset

    def advance(self, seconds: int) -> datetime:
        self.offset += seconds
        return self.now()

    def invalidate(self) -> None:
        self.invalidated += 1


TEST_PATHS = [
    ("post", "/v1/test/clock/advance", {"json": {"seconds": 60}}),
    ("get", "/v1/test/clock", {}),
    ("post", "/v1/test/reset", {}),
]


@pytest.mark.parametrize(
    "settings",
    [
        Settings(enable_test_routes=False),
        # Production ignores ENABLE_TEST_ROUTES: the routes don't exist at all.
        Settings(enable_test_routes=True, app_env=AppEnv.PRODUCTION),
    ],
    ids=["disabled", "production"],
)
@pytest.mark.parametrize(("method", "path", "kwargs"), TEST_PATHS)
def test_routes_are_404_unless_enabled_outside_production(settings, method, path, kwargs):
    app = create_app(settings)
    app.dependency_overrides[get_principal] = lambda: user_principal(Role.ADMIN)
    assert getattr(TestClient(app), method)(path, **kwargs).status_code == 404
    assert not any(p.startswith("/v1/test") for p in app.openapi()["paths"])


@pytest.fixture
def api():
    clock = FakeSimulatedClock()
    state = {"principal": user_principal(Role.ADMIN)}
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    app.dependency_overrides.update(
        {
            get_principal: lambda: state["principal"],
            get_simulated_clock: lambda: clock,
            get_uow: lambda: FakeUnitOfWork(),
        }
    )
    return TestClient(app), clock, state


def test_clock_advance(api):
    client, clock, _ = api
    resp = client.post("/v1/test/clock/advance", json={"seconds": 900})
    assert resp.status_code == 200
    assert resp.json() == {"simulated_time": "2026-10-18T12:15:00Z", "offset_seconds": 900}
    assert client.get("/v1/test/clock").json()["offset_seconds"] == 900


def test_clock_advance_accepts_integral_json_numbers(api):
    # JSON Schema counts 60.0 as an integer (found by fuzzing against the pod interface).
    client, clock, _ = api
    assert client.post("/v1/test/clock/advance", json={"seconds": 60.0}).status_code == 200
    assert clock.offset == 60


@pytest.mark.parametrize("seconds", [-1, 1.5, "60", True, None, 10 * 366 * 24 * 3600 + 1])
def test_clock_advance_rejects_invalid_seconds(api, seconds):
    client, clock, _ = api
    assert client.post("/v1/test/clock/advance", json={"seconds": seconds}).status_code == 400
    assert clock.offset == 0


@pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER, Role.SYSTEM])
@pytest.mark.parametrize(("method", "path", "kwargs"), TEST_PATHS)
def test_admin_only(api, monkeypatch, role, method, path, kwargs):
    client, clock, state = api
    monkeypatch.setattr(testing_router, "reset_test_environment", lambda uow: None)
    state["principal"] = user_principal(role)
    assert getattr(client, method)(path, **kwargs).status_code == 403
    assert clock.offset == 0


def test_reset_route_resets_and_refreshes_the_clock(api, monkeypatch):
    client, clock, _ = api
    calls = []
    monkeypatch.setattr(testing_router, "reset_test_environment", calls.append)
    resp = client.post("/v1/test/reset")
    assert resp.status_code == 204 and resp.content == b""
    assert len(calls) == 1 and clock.invalidated == 1


def test_reset_service_refuses_when_test_routes_are_off(monkeypatch):
    from app.modules.testing import service

    monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=False))
    with pytest.raises(ResetNotAllowed):
        reset_test_environment(FakeUnitOfWork())
