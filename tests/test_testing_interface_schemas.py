"""Schemathesis: the system's /test/* and the tester service against their OpenAPI documents
(docs/testing-interface/system-openapi.yaml and tester-openapi.yaml).

Requests are generated from OUR documents and every response is validated against them, so
drift between an implementation and its document fails. The system's reset and coverage meter
and the tester's target and suite runner are doubles (the real ones are covered by
tests/test_test_sessions.py, tests/test_tester.py and the CI self-test).
"""

import os
from pathlib import Path

import pytest
import schemathesis
import yaml
from hypothesis import HealthCheck, settings

from app.core.config import AppEnv, Settings
from app.db.uow import get_uow
from app.main import create_app
from app.modules.testing import session_router
from app.modules.testing.clock import get_simulated_clock
from app.modules.testing.sessions import TestSessions, get_test_sessions
from tester import main as tester_main
from tests.fakes import FakeUnitOfWork
from tests.test_test_sessions import FakeMeter
from tests.test_tester import Target, scripted_run
from tests.test_testing_routes import FakeSimulatedClock

DOCS = Path(__file__).resolve().parents[1] / "docs" / "testing-interface"
FUZZ = settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)


def _load(name: str):
    return schemathesis.openapi.from_dict(yaml.safe_load((DOCS / name).read_text("utf-8")))


def _system_app():
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    sessions, clock = TestSessions(FakeMeter()), FakeSimulatedClock()
    app.dependency_overrides.update(
        {
            get_test_sessions: lambda: sessions,
            get_simulated_clock: lambda: clock,
            get_uow: lambda: FakeUnitOfWork(),
        }
    )
    return app


@pytest.fixture
def tester_doubles():
    """Installed per test: tester.main.app is shared with tests/test_tester.py, whose fixture
    clears its overrides afterwards."""
    import httpx

    target, sessions = Target(), tester_main.Sessions()

    def client():
        with httpx.Client(transport=httpx.MockTransport(target.handle)) as c:
            yield c

    overrides = tester_main.app.dependency_overrides
    saved = dict(overrides)
    overrides.update(
        {
            tester_main.get_sessions: lambda: sessions,
            tester_main.get_target_client: client,
            tester_main.get_runner: lambda: lambda target_url, overrides: scripted_run(),
        }
    )
    yield
    overrides.clear()
    overrides.update(saved)


system_schema = _load("system-openapi.yaml")
system_schema.app = _system_app()
tester_schema = _load("tester-openapi.yaml")
tester_schema.app = tester_main.app


@pytest.fixture
def no_database_reset(monkeypatch):
    monkeypatch.setattr(session_router, "reset_test_environment", lambda uow: None)


@pytest.mark.fuzz
@system_schema.parametrize()
@FUZZ
def test_system_testing_interface_conforms(case, no_database_reset):
    case.call_and_validate()


@pytest.mark.fuzz
@tester_schema.parametrize()
@FUZZ
def test_tester_conforms(case, tester_doubles):
    case.call_and_validate()
