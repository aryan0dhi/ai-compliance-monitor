"""Schemathesis fuzzing of /v1 against real PostgreSQL (complements tests/test_fuzz.py).

The in-memory fakes can't surface driver, encoding or constraint errors; this runs the same
schema-driven requests through the production unit of work and SQL repositories on a throwaway
database. Auth, ledger and notifier are still the explicit doubles (their modules aren't built).

Run inside docker compose: pytest -m "integration and fuzz"
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import schemathesis
from fastapi import Request
from hypothesis import HealthCheck, reject, settings

from app.core.clock import get_clock
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.interface import get_notifier
from tests.conftest import T0
from tests.fakes import (
    FixedClock,
    RecordingLedger,
    RecordingNotifier,
    system_principal,
    user_principal,
)

pytestmark = pytest.mark.fuzz

SYSTEM_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_STATE: dict = {}


def _principal(request: Request):
    return system_principal(SYSTEM_ID) if request.method == "POST" else user_principal(Role.VIEWER)


APP = create_app()
APP.dependency_overrides.update(
    {
        get_principal: _principal,
        get_uow: lambda: SqlAlchemyUnitOfWork(_STATE["sessionmaker"]),
        get_ledger: lambda: RecordingLedger(),
        get_notifier: lambda: RecordingNotifier(),
        get_clock: lambda: FixedClock(T0),
    }
)
schema = schemathesis.openapi.from_asgi("/openapi.json", APP).include(path_regex="^/v1/")


@pytest.fixture(autouse=True)
def _real_database(pg_sessionmaker, seed):
    seed(SYSTEM_ID)
    _STATE["sessionmaker"] = pg_sessionmaker
    yield
    _STATE.clear()


@schema.parametrize()
@settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
def test_api_on_postgres_never_errors_and_matches_schema(case):
    # Same documented exclusion as tests/test_fuzz.py: schema-valid date-times outside Python's
    # range once converted to UTC are an intentional 400.
    if isinstance(case.body, dict) and isinstance(case.body.get("timestamp"), str):
        try:
            datetime.fromisoformat(case.body["timestamp"]).astimezone(UTC)
        except (ValueError, OverflowError):
            reject()
    case.call_and_validate()


def test_seeded_threshold_is_reachable():
    """Guard: the fuzz app really talks to the seeded database (not an empty one)."""
    from fastapi.testclient import TestClient

    body = {
        "system_id": str(SYSTEM_ID),
        "metric_name": "drift_score",
        "metric_type": "drift",
        "value": 0.35,
        "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
    }
    resp = TestClient(APP).post("/v1/metrics", json=body)
    assert resp.status_code == 201 and "flagged_event_id" in resp.json()
