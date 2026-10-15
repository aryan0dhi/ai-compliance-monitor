"""Unimplemented integration points must fail loudly, and auth must fail closed."""

from uuid import uuid4

import pytest
from fastapi import Depends, HTTPException
from fastapi.testclient import TestClient

from app.core.errors import UnimplementedError
from app.main import create_app
from app.modules.auth.interface import Principal, get_principal
from app.modules.ledger import interface as ledger
from app.modules.notifications import interface as notifications


def test_auth_fails_closed():
    with pytest.raises(HTTPException) as exc:
        get_principal()
    assert exc.value.status_code == 401


def test_protected_route_is_denied_until_auth_exists():
    app = create_app()

    @app.get("/v1/_probe")
    def probe(principal: Principal = Depends(get_principal)):
        return {"reached": True}

    resp = TestClient(app).get("/v1/_probe", headers={"Authorization": "Bearer anything"})
    assert resp.status_code == 401
    assert "not implemented" in resp.json()["detail"]


def test_ledger_append_is_unimplemented():
    with pytest.raises(UnimplementedError):
        ledger.append(None, entry_type="FLAGGED", actor_id=None, payload={})


def test_notifications_enqueue_is_unimplemented():
    with pytest.raises(UnimplementedError):
        notifications.enqueue(
            None, notification_type="flag_notification", event_id=uuid4(), payload={}
        )


def test_unimplemented_error_maps_to_501():
    app = create_app()

    @app.get("/v1/_probe")
    def probe():
        ledger.append(None, entry_type="VIEW", actor_id=None, payload={})

    resp = TestClient(app).get("/v1/_probe")
    assert resp.status_code == 501
    assert "app/modules/ledger" in resp.json()["detail"]
