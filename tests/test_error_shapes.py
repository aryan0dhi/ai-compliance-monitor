"""B-22: every error status is JSON {"detail": ...} that never echoes the request."""

import ipaddress
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.core.errors import UnimplementedError
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.delivery import HttpWebhookSender
from app.modules.notifications.interface import get_notifier
from app.modules.notifications.router import get_webhook_sender
from tests.conftest import T0
from tests.contract.scenarios import assert_error_body
from tests.fakes import system_principal, user_principal
from tests.test_api import SYSTEM_ID, VALID_METRIC

SENTINEL = "sentinel-7f3a"
WEBHOOK = {"url": f"https://{SENTINEL}.example.com/hook", "event_types": ["flag_notification"]}


def failing_sender() -> HttpWebhookSender:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    return HttpWebhookSender(
        timeout_s=1,
        allow_private=False,
        require_https=False,
        resolver=lambda target, allow_private: [ipaddress.ip_address("93.184.215.14")],
        transport=httpx.MockTransport(refuse),
    )


@pytest.fixture
def env(uow, ledger, notifier, clock):
    uow.seed_system(SYSTEM_ID)
    for name, kind in ((VALID_METRIC["metric_name"], "drift"), (SENTINEL, "drift")):
        uow.seed_threshold(
            SYSTEM_ID,
            name,
            metric_type=kind,
            direction="above",
            limit="0.30",
            valid_from=T0 - timedelta(days=1),
        )
    app = create_app()
    state = {"principal": system_principal(SYSTEM_ID)}
    app.dependency_overrides.update(
        {
            get_principal: lambda: state["principal"],
            get_uow: lambda: uow,
            get_ledger: lambda: ledger,
            get_notifier: lambda: notifier,
            get_clock: lambda: clock,
            get_webhook_sender: failing_sender,
        }
    )

    def boom():
        raise RuntimeError(SENTINEL)

    def not_built():
        raise UnimplementedError("the thing", "REQ-99", "nowhere")

    app.add_api_route("/v1/_boom", boom)
    app.add_api_route("/v1/_not_built", not_built)
    return TestClient(app, raise_server_exceptions=False), state


def check(resp, status):
    assert resp.status_code == status, resp.text
    assert_error_body(resp)
    assert SENTINEL not in resp.text
    return resp


class TestErrorShapes:
    def test_400_malformed_json(self, env):
        client, _ = env
        resp = client.post(
            "/v1/metrics", content=b"{", headers={"Content-Type": "application/json"}
        )
        check(resp, 400)

    def test_400_schema_violation_lists_loc_msg_type_only(self, env):
        client, _ = env
        body = {**VALID_METRIC, "metric_type": SENTINEL, "value": SENTINEL, "extra": SENTINEL}
        detail = check(client.post("/v1/metrics", json=body), 400).json()["detail"]
        assert all(set(e) == {"loc", "msg", "type"} for e in detail)

    def test_400_bad_query(self, env):
        client, state = env
        state["principal"] = user_principal(Role.VIEWER)
        check(client.get("/v1/events", params={"limit": SENTINEL}), 400)

    def test_400_unknown_system(self, env):
        client, state = env
        other = uuid4()
        state["principal"] = system_principal(other)
        resp = client.post("/v1/metrics", json={**VALID_METRIC, "system_id": str(other)})
        assert str(other) not in check(resp, 400).text

    def test_400_metric_type_mismatch(self, env):
        client, _ = env
        body = {**VALID_METRIC, "metric_name": SENTINEL, "metric_type": "performance"}
        check(client.post("/v1/metrics", json=body), 400)

    def test_400_disallowed_webhook_url(self, env):
        client, state = env
        state["principal"] = user_principal(Role.ADMIN)
        check(client.post("/v1/webhooks", json={**WEBHOOK, "url": f"ftp://{SENTINEL}/"}), 400)

    def test_401_keeps_its_challenge_header(self, env):
        client, state = env

        def refuse():
            from fastapi import HTTPException

            raise HTTPException(401, "Missing or invalid API key", {"WWW-Authenticate": "Bearer"})

        client.app.dependency_overrides[get_principal] = refuse
        assert check(client.get("/v1/events"), 401).headers["www-authenticate"] == "Bearer"

    def test_403(self, env):
        client, state = env
        state["principal"] = user_principal(Role.VIEWER)
        check(client.post("/v1/metrics", json=VALID_METRIC), 403)

    @pytest.mark.parametrize("path", ["/v1/events/" + SENTINEL, "/v1/" + SENTINEL])
    def test_404_unknown_event_and_unknown_route(self, env, path):
        client, state = env
        state["principal"] = user_principal(Role.VIEWER)
        check(client.get(path), 404)

    def test_405(self, env):
        client, state = env
        state["principal"] = user_principal(Role.VIEWER)
        check(client.delete("/v1/events"), 405)

    def test_409_conflicting_duplicate(self, env):
        client, _ = env
        assert client.post("/v1/metrics", json=VALID_METRIC).status_code == 201
        check(client.post("/v1/metrics", json={**VALID_METRIC, "value": 0.1}), 409)

    def test_422_failed_ping_does_not_leak_the_url(self, env):
        client, state = env
        state["principal"] = user_principal(Role.ADMIN)
        resp = check(client.post("/v1/webhooks", json=WEBHOOK), 422)
        assert resp.json()["detail"] == "verification ping failed: connection failed"

    def test_500_is_json_without_the_exception(self, env):
        client, _ = env
        resp = check(client.get("/v1/_boom"), 500)
        assert resp.json() == {"detail": "internal server error"}

    def test_501(self, env):
        client, _ = env
        check(client.get("/v1/_not_built"), 501)
