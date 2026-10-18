"""Schemathesis fuzzing of /v1 against real PostgreSQL (complements tests/test_fuzz.py).

The in-memory fakes can't surface driver, encoding or constraint errors; this runs the same
schema-driven requests through the production wiring on a throwaway database: real API-key
authentication, the hash-chained ledger and the notification outbox. Only the webhook sender (no
network) and the route clock are doubles.

Run inside docker compose: pytest -m "integration and fuzz"
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import schemathesis
from hypothesis import HealthCheck, reject, settings

from app.core.clock import get_clock
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, hash_api_key
from app.modules.auth.models import ApiKey, AppUser
from app.modules.notifications.router import get_webhook_sender
from tests.conftest import T0
from tests.fakes import FixedClock, RecordingSender

pytestmark = pytest.mark.fuzz

SYSTEM_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
_STATE: dict = {}

APP = create_app()
APP.dependency_overrides.update(
    {
        get_uow: lambda: SqlAlchemyUnitOfWork(_STATE["sessionmaker"]),
        get_clock: lambda: FixedClock(T0),
        get_webhook_sender: lambda: RecordingSender(),
    }
)
schema = schemathesis.openapi.from_asgi("/openapi.json", APP).include(path_regex="^/v1/")


# Fixed keys (test database only), so the auth provider needs no per-test state: Schemathesis
# may ask for credentials while collecting tests, before fixtures run.
FUZZ_KEYS = {
    Role.SYSTEM: "acm_fuzz-system-key",
    Role.ADMIN: "acm_fuzz-admin-key",
    Role.VIEWER: "acm_fuzz-viewer-key",
}


@schema.auth()
class RealKeys:
    """A real API key with the role each endpoint needs (system / admin / viewer)."""

    def get(self, case, ctx):
        if case.path == "/v1/metrics":
            return FUZZ_KEYS[Role.SYSTEM]
        if case.path == "/v1/webhooks":
            return FUZZ_KEYS[Role.ADMIN]
        return FUZZ_KEYS[Role.VIEWER]

    def set(self, case, data, ctx):
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}


@pytest.fixture(autouse=True)
def _real_database(pg_sessionmaker, seed):
    seed(SYSTEM_ID)
    with pg_sessionmaker.begin() as session:
        for role, key in FUZZ_KEYS.items():
            user = AppUser(
                id=uuid4(),
                name=f"fuzz-{role}",
                role=role.value,
                system_id=SYSTEM_ID if role is Role.SYSTEM else None,
            )
            session.add(user)
            session.flush()
            session.add(
                ApiKey(id=uuid4(), user_id=user.id, key_hash=hash_api_key(key), key_prefix=key[:10])
            )
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
    """Guard: the fuzz app really talks to the seeded database, with real auth and ledger."""
    from fastapi.testclient import TestClient

    body = {
        "system_id": str(SYSTEM_ID),
        "metric_name": "drift_score",
        "metric_type": "drift",
        "value": 0.35,
        "timestamp": (T0 + timedelta(seconds=1)).isoformat(),
    }
    headers = {"Authorization": f"Bearer {FUZZ_KEYS[Role.SYSTEM]}"}
    resp = TestClient(APP).post("/v1/metrics", json=body, headers=headers)
    assert resp.status_code == 201 and "flagged_event_id" in resp.json()
    viewer = {"Authorization": f"Bearer {FUZZ_KEYS[Role.VIEWER]}"}
    assert TestClient(APP).get("/v1/ledger/verify", headers=viewer).json()["valid"] is True
