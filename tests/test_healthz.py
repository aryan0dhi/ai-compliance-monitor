from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import health
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_ok_when_database_reachable(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", lambda: "ok")
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok", "worker": "ok"}


def test_healthz_503_when_database_unreachable(client, monkeypatch):
    def fail():
        raise ConnectionError("db down")

    monkeypatch.setattr(health, "check_database", fail)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "unavailable", "worker": "unknown"}


@pytest.mark.parametrize("worker", ["stale", "missing"])
def test_stopped_worker_is_reported_but_stays_200(client, monkeypatch, worker):
    # Restarting the API container can't fix the worker, so the health check must not fail.
    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", lambda: worker)
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json()["worker"] == worker


def test_worker_check_failure_is_unknown(client, monkeypatch):
    def fail():
        raise RuntimeError("no heartbeat table")

    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", fail)
    assert client.get("/healthz").json()["worker"] == "unknown"


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("beat_at", "expected"),
    [
        (None, "missing"),
        (NOW - timedelta(seconds=60), "ok"),
        (NOW - timedelta(seconds=61), "stale"),
    ],
)
def test_classify_heartbeat(beat_at, expected):
    assert health.classify_heartbeat(beat_at, NOW, timedelta(seconds=60)) == expected


@pytest.mark.integration
def test_healthz_against_real_database(client):
    """Runs inside docker compose, where the db service is available."""
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.json()
