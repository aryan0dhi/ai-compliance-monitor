import pytest
from fastapi.testclient import TestClient

from app.api import health
from app.main import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_ok_when_database_reachable(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", lambda: None)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "ok"}


def test_healthz_503_when_database_unreachable(client, monkeypatch):
    def fail():
        raise ConnectionError("db down")

    monkeypatch.setattr(health, "check_database", fail)
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "database": "unavailable"}


@pytest.mark.integration
def test_healthz_against_real_database(client):
    """Runs inside docker compose, where the db service is available."""
    resp = client.get("/healthz")
    assert resp.status_code == 200, resp.json()
