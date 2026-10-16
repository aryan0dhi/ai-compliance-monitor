import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from tests.fakes import FakeUnitOfWork, FixedClock, RecordingLedger, RecordingNotifier

T0 = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)

# --- integration test gating -------------------------------------------------------------------
# Tests marked `integration` (and everything under tests/integration/) need a reachable
# PostgreSQL. Without one they are SKIPPED, unless REQUIRE_POSTGRES=1 is set (CI, docker compose),
# in which case they FAIL so a missing database can't go unnoticed.


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session")
def postgres_server_url():
    """SQLAlchemy URL of the configured PostgreSQL server (from the app's env settings)."""
    url = make_url(get_settings().sqlalchemy_url)
    probe = create_engine(url.set(database="postgres"), connect_args={"connect_timeout": 3})
    try:
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        message = f"PostgreSQL not reachable at {url.host}:{url.port} ({type(exc).__name__})"
        if os.environ.get("REQUIRE_POSTGRES") == "1":
            pytest.fail(message)
        pytest.skip(message)
    finally:
        probe.dispose()
    return url


@pytest.fixture(autouse=True)
def _require_postgres_for_integration(request):
    if request.node.get_closest_marker("integration"):
        request.getfixturevalue("postgres_server_url")


# --- unit test doubles -------------------------------------------------------------------------


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def ledger() -> RecordingLedger:
    return RecordingLedger()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0.replace(minute=1))
