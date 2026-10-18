"""Production wiring of the integration points: auth fails closed, and the ledger and notifier
are the real implementations, which refuse to run outside the caller's transaction."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app.core.errors import UnimplementedError
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Principal, Role, get_principal, hash_api_key
from app.modules.auth.service import create_key, create_user, revoke_key
from app.modules.ledger.interface import get_ledger
from app.modules.ledger.writer import SqlLedgerWriter
from app.modules.notifications.interface import get_notifier
from app.modules.notifications.outbox import OutboxNotifier
from tests.fakes import FakeUnitOfWork, FixedClock


@pytest.fixture
def probe_client(uow):
    app = create_app()

    @app.get("/v1/_probe")
    def probe(principal: Principal = Depends(get_principal)):
        return {"actor_id": str(principal.actor_id), "role": principal.role}

    app.dependency_overrides[get_uow] = lambda: uow
    return TestClient(app)


class TestAuthentication:
    @pytest.mark.parametrize(
        "headers",
        [
            {},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer "},
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Bearer acm_not-a-real-key"},
            {"Authorization": "acm_missing-scheme"},
        ],
    )
    def test_bad_or_missing_key_is_401(self, probe_client, headers):
        resp = probe_client.get("/v1/_probe", headers=headers)
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"] == "Bearer"
        assert resp.json() == {"detail": "Missing or invalid API key"}

    def test_valid_key_resolves_principal(self, probe_client, uow):
        issued = create_user(uow, name="alice", role=Role.REVIEWER)
        resp = probe_client.get("/v1/_probe", headers={"Authorization": f"Bearer {issued.api_key}"})
        assert resp.status_code == 200
        assert resp.json() == {"actor_id": str(issued.user_id), "role": "reviewer"}

    def test_revoked_key_is_401_and_other_keys_still_work(self, probe_client, uow):
        first = create_user(uow, name="bob", role=Role.VIEWER)
        second = create_key(uow, user_name="bob")
        revoke_key(uow, key_id=first.key_id, clock=FixedClock(datetime(2026, 10, 1, tzinfo=UTC)))
        old = probe_client.get("/v1/_probe", headers={"Authorization": f"Bearer {first.api_key}"})
        new = probe_client.get("/v1/_probe", headers={"Authorization": f"Bearer {second.api_key}"})
        assert (old.status_code, new.status_code) == (401, 200)

    def test_system_key_carries_its_system_id(self, probe_client, uow):
        system_id = uow.seed_system()
        issued = create_user(uow, name="sys", role=Role.SYSTEM, system_id=system_id)
        principal = uow.auth.principal_for_key(hash_api_key(issued.api_key))
        assert principal.system_id == system_id and principal.role is Role.SYSTEM

    def test_only_the_hash_is_stored(self, uow):
        issued = create_user(uow, name="carol", role=Role.ADMIN)
        (key,) = uow.auth.keys.values()
        assert key.key_hash == hash_api_key(issued.api_key) != issued.api_key
        assert issued.api_key.startswith(key.key_prefix) and len(key.key_prefix) < 16
        assert issued.api_key not in repr(vars(key))


def test_production_wiring_uses_real_integrations():
    # Guards against a test double or no-op accidentally becoming the production default.
    assert type(get_ledger()) is SqlLedgerWriter
    assert type(get_notifier()) is OutboxNotifier


def test_real_ledger_and_notifier_require_the_callers_session():
    # A unit of work without a database session (the in-memory fake) can't reach them silently.
    with pytest.raises(RuntimeError, match="session"):
        get_ledger().append(None, entry_type="FLAGGED", actor_id=None, payload={})
    with pytest.raises(RuntimeError, match="session"):
        get_notifier().enqueue(
            None, notification_type="flag_notification", event_id=uuid4(), payload={}
        )


def test_unimplemented_error_maps_to_501():
    # Kept for integration points added in later phases.
    app = create_app()

    @app.get("/v1/_probe")
    def probe():
        raise UnimplementedError("Report assembly", "REQ-12", "reports")

    resp = TestClient(app).get("/v1/_probe")
    assert resp.status_code == 501
    assert "app/modules/reports" in resp.json()["detail"]


def test_fake_uow_restores_mutated_objects_on_rollback():
    uow = FakeUnitOfWork()
    system_id = uow.seed_system()
    with pytest.raises(ValueError), uow:
        uow.systems.rows[system_id].gap_missed_intervals = 9
        raise ValueError
    assert uow.systems.rows[system_id].gap_missed_intervals == 3
