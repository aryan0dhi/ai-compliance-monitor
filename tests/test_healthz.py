from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import health
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


PLATFORM = {
    "last_verification": {
        "finished_at": "2026-10-18T12:00:00Z",
        "valid": True,
        "head_check": "MATCH",
    },
    "last_verification_age_seconds": 30,
    "reset_count": 2,
}


def test_healthz_ok_when_database_reachable(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", lambda: "ok")
    monkeypatch.setattr(health, "_platform_status", lambda: dict(PLATFORM))
    monkeypatch.setenv("APP_VERSION", "1.4.0")
    health.get_settings.cache_clear()
    try:
        resp = client.get("/healthz")
    finally:
        health.get_settings.cache_clear()
    assert resp.status_code == 200
    # Contract delta A1 adds version, last_verification(_age_seconds) and reset_count (D-29).
    assert resp.json() == {
        "status": "ok",
        "database": "ok",
        "worker": "ok",
        "version": "1.4.0",
        **PLATFORM,
    }


def test_healthz_version_is_null_when_unset(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", lambda: "ok")
    monkeypatch.setattr(health, "_platform_status", lambda: dict(PLATFORM))
    monkeypatch.delenv("APP_VERSION", raising=False)
    health.get_settings.cache_clear()
    try:
        assert client.get("/healthz").json()["version"] is None
    finally:
        health.get_settings.cache_clear()


def test_healthz_503_when_database_unreachable(client, monkeypatch):
    def fail():
        raise ConnectionError("db down")

    monkeypatch.setattr(health, "check_database", fail)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    # A1: the 503 body has the same keys, version from the environment, the other three null.
    assert resp.json() == {
        "status": "degraded",
        "database": "unavailable",
        "worker": "unknown",
        "version": None,
        "last_verification": None,
        "last_verification_age_seconds": None,
        "reset_count": None,
    }


def test_platform_status_failure_is_null_but_stays_200(client, monkeypatch):
    def fail():
        raise RuntimeError("no ledger_check table")

    monkeypatch.setattr(health, "check_database", lambda: None)
    monkeypatch.setattr(health, "worker_status", lambda: "ok")
    monkeypatch.setattr(health, "_platform_status", fail)
    resp = client.get("/healthz")
    assert resp.status_code == 200 and resp.json()["reset_count"] is None


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
