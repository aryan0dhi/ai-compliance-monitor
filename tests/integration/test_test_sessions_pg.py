"""POST /test/setup and GET /test/report on PostgreSQL: setup really resets the test database.

The coverage meter is a double (a real one can't share the process with pytest-cov); its real
behaviour is checked in a clean process by tests/test_test_sessions.py.
"""

from fastapi.testclient import TestClient

from app.core.config import AppEnv, Settings
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, hash_api_key
from app.modules.auth.service import create_user
from app.modules.events.models import Event
from app.modules.metrics.models import MetricSample
from app.modules.systems.models import MonitoredSystem
from app.modules.testing.clock import SimulatedClock, get_simulated_clock
from app.modules.testing.fixtures import BASELINE_SYSTEMS
from app.modules.testing.sessions import TestSessions, get_test_sessions
from tests.test_test_sessions import FakeMeter


def test_setup_resets_to_the_baseline_and_report_follows_the_session(
    pg_sessionmaker, seed, monkeypatch
):
    from app.modules.testing import service

    monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=True))
    make_uow = lambda: SqlAlchemyUnitOfWork(pg_sessionmaker)  # noqa: E731
    admin = create_user(make_uow(), name="tester", role=Role.ADMIN)
    loan = BASELINE_SYSTEMS[0]
    seed(loan.id)
    metric = {
        "system_id": str(loan.id),
        "metric_name": "drift_score",
        "metric_type": "drift",
        "value": 0.35,
        "timestamp": "2026-10-18T12:00:00Z",
    }
    sim = SimulatedClock(pg_sessionmaker, cache_ttl_s=0)
    sim.advance(3600)
    sessions = TestSessions(FakeMeter())
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    app.dependency_overrides.update(
        {
            get_uow: make_uow,
            get_simulated_clock: lambda: sim,
            get_test_sessions: lambda: sessions,
        }
    )
    client = TestClient(app)
    system_key = create_user(make_uow(), name="loan", role=Role.SYSTEM, system_id=loan.id)
    headers = {"Authorization": f"Bearer {system_key.api_key}"}
    assert client.post("/v1/metrics", json=metric, headers=headers).status_code == 201

    resp = client.post("/test/setup", json={"session_id": "grader-1", "setup": {}})
    assert resp.status_code == 201 and resp.json() == {"session_id": "grader-1"}
    with pg_sessionmaker() as s:
        assert s.query(Event).count() == 0 and s.query(MetricSample).count() == 0
        assert s.get(MonitoredSystem, loan.id).name == loan.name
    assert sim.offset_seconds() == 0
    with make_uow() as uow:  # keys survive, so the tester can keep using them
        assert uow.auth.principal_for_key(hash_api_key(admin.api_key)) is not None

    report = client.get("/test/report", params={"session_id": "grader-1"})
    assert report.status_code == 200
    assert report.json()["coverage"]["code"]["basis"] == "fake meter"
    assert client.post("/test/setup", json={"session_id": "grader-1"}).status_code == 409
    assert client.get("/test/report", params={"session_id": "grader-2"}).status_code == 404
