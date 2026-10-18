"""Webhook targets: SSRF guard, address pinning, signatures, and POST /v1/webhooks (REQ-04)."""

import hashlib
import hmac
import ipaddress
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.notifications.delivery import (
    SIGNATURE_HEADER,
    HttpWebhookSender,
    SendResult,
    UnsafeWebhookUrl,
    is_public_address,
    parse_webhook_url,
    resolve_addresses,
    sign,
)
from app.modules.notifications.router import get_webhook_sender
from tests.fakes import RecordingSender, system_principal, user_principal

PUBLIC = ipaddress.ip_address("93.184.215.14")


class TestUrlRules:
    @pytest.mark.parametrize(
        ("url", "host", "port", "path"),
        [
            ("https://hooks.example.com/r?x=1", "hooks.example.com", 443, "/r?x=1"),
            ("http://hooks.example.com", "hooks.example.com", 80, "/"),
            ("https://hooks.example.com:8443/a", "hooks.example.com", 8443, "/a"),
            ("https://[2001:db8::1]/a", "2001:db8::1", 443, "/a"),
        ],
    )
    def test_valid(self, url, host, port, path):
        target = parse_webhook_url(url, require_https=False)
        assert (target.host, target.port, target.path_and_query) == (host, port, path)

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://hooks.example.com/",
            "file:///etc/passwd",
            "gopher://127.0.0.1:6379/",
            "https://user:pass@hooks.example.com/",
            "https://hooks.example.com@evil.example/",
            "https:///nohost",
            "https://hooks.example.com:99999/",
            "https://hooks.example.com/" + "a" * 2048,
        ],
    )
    def test_invalid(self, url):
        with pytest.raises(UnsafeWebhookUrl):
            parse_webhook_url(url, require_https=False)

    def test_production_requires_https(self):
        with pytest.raises(UnsafeWebhookUrl, match="https"):
            parse_webhook_url("http://hooks.example.com/", require_https=True)

    @pytest.mark.parametrize(
        "address",
        [
            "127.0.0.1",
            "10.0.0.5",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",  # cloud instance metadata
            "100.64.0.1",  # carrier-grade NAT
            "0.0.0.0",
            "224.0.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",  # IPv4-mapped loopback
            "::ffff:169.254.169.254",
        ],
    )
    def test_non_public_addresses(self, address):
        assert not is_public_address(ipaddress.ip_address(address))

    @pytest.mark.parametrize("address", ["93.184.215.14", "8.8.8.8", "2606:4700:4700::1111"])
    def test_public_addresses(self, address):
        assert is_public_address(ipaddress.ip_address(address))


def fake_getaddrinfo(*addresses):
    def getaddrinfo(host, port, *args, **kwargs):
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in addresses
        ]

    return getaddrinfo


class TestResolution:
    target = parse_webhook_url("https://hooks.example.com/", require_https=True)

    def test_any_private_answer_rejects_the_host(self, monkeypatch):
        # A host with one public and one private record could be steered to the private one.
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.215.14", "10.0.0.1"))
        with pytest.raises(UnsafeWebhookUrl, match="private"):
            resolve_addresses(self.target, allow_private=False)

    def test_public_answers_are_returned(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo("93.184.215.14", "fe80::1%3"))
        assert resolve_addresses(self.target, allow_private=True) == [
            PUBLIC,
            ipaddress.ip_address("fe80::1"),
        ]

    def test_unresolvable_host(self, monkeypatch):
        def fail(*args, **kwargs):
            raise socket.gaierror("no such host")

        monkeypatch.setattr(socket, "getaddrinfo", fail)
        with pytest.raises(UnsafeWebhookUrl, match="resolved"):
            resolve_addresses(self.target, allow_private=False)


class TestSender:
    def sender(self, handler, **kwargs):
        return HttpWebhookSender(
            timeout_s=2,
            allow_private=False,
            require_https=False,
            resolver=lambda target, allow_private: [PUBLIC],
            transport=httpx.MockTransport(handler),
            **kwargs,
        )

    def test_connects_to_the_vetted_address_with_original_host_and_signature(self):
        seen = {}

        def handler(request):
            seen["request"] = request
            return httpx.Response(204)

        body = b'{"a":1}'
        result = self.sender(handler).send("https://hooks.example.com:8443/r?x=1", body, "s3cret")
        assert result == SendResult(ok=True, status_code=204, error=None)
        request = seen["request"]
        assert str(request.url) == "https://93.184.215.14:8443/r?x=1"
        assert request.headers["Host"] == "hooks.example.com:8443"
        assert request.extensions["sni_hostname"] == "hooks.example.com"
        expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        assert request.headers[SIGNATURE_HEADER] == expected == sign(body, "s3cret")
        assert request.content == body

    @pytest.mark.parametrize("status", [301, 302, 307, 400, 500])
    def test_non_2xx_fails_and_redirects_are_not_followed(self, status):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(status, headers={"Location": "http://169.254.169.254/"})

        result = self.sender(handler).send("https://hooks.example.com/", b"{}", "s")
        assert (result.ok, result.status_code, result.error) == (False, status, f"HTTP {status}")
        assert len(calls) == 1

    def test_network_error_is_reported_not_raised(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        result = self.sender(handler).send("https://hooks.example.com/", b"{}", "s")
        assert not result.ok and result.status_code is None and "ConnectError" in result.error

    def test_falls_back_to_the_next_address_only_when_connecting_fails(self):
        v6, v4 = ipaddress.ip_address("2606:4700:4700::1111"), PUBLIC
        tried = []

        def handler(request):
            tried.append(request.url.host)
            if request.url.host == str(v6):
                raise httpx.ConnectError("network unreachable")
            return httpx.Response(200)

        sender = HttpWebhookSender(
            timeout_s=2,
            allow_private=False,
            require_https=False,
            resolver=lambda target, allow_private: [v6, v4],
            transport=httpx.MockTransport(handler),
        )
        assert sender.send("https://hooks.example.com/", b"{}", "s").ok
        assert tried == [str(v6), str(v4)]

    def test_read_timeout_is_final_so_the_request_is_not_sent_twice(self):
        tried = []

        def handler(request):
            tried.append(request.url.host)
            raise httpx.ReadTimeout("slow receiver")

        sender = HttpWebhookSender(
            timeout_s=2,
            allow_private=False,
            require_https=False,
            resolver=lambda target, allow_private: [PUBLIC, ipaddress.ip_address("8.8.8.8")],
            transport=httpx.MockTransport(handler),
        )
        result = sender.send("https://hooks.example.com/", b"{}", "s")
        assert not result.ok and "ReadTimeout" in result.error and len(tried) == 1

    def test_unsafe_target_is_never_contacted(self):
        def handler(request):  # pragma: no cover - must not be reached
            raise AssertionError("request sent")

        sender = HttpWebhookSender(
            timeout_s=2,
            allow_private=False,
            require_https=False,
            transport=httpx.MockTransport(handler),
        )
        result = sender.send("http://127.0.0.1:9/", b"{}", "s")
        assert not result.ok and "private" in result.error


class _Receiver(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        self.received.append((dict(self.headers), self.rfile.read(length)))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def test_real_http_delivery_to_a_local_receiver():
    """A real socket: the request reaches the pinned address with the right Host and signature."""
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        body = json.dumps({"notification_id": "n1"}).encode()
        sender = HttpWebhookSender(timeout_s=5, allow_private=True, require_https=False)
        result = sender.send(f"http://localhost:{port}/hook", body, "k")
        assert result.ok and result.status_code == 200
        headers, received = _Receiver.received[-1]
        assert received == body
        assert headers["Host"] == f"localhost:{port}"
        assert headers[SIGNATURE_HEADER] == sign(body, "k")
        # The same URL is refused when private networks aren't allowed (the default).
        strict = HttpWebhookSender(timeout_s=5, allow_private=False, require_https=False)
        assert not strict.send(f"http://localhost:{port}/hook", body, "k").ok
    finally:
        server.shutdown()


class TestRegisterRoute:
    @pytest.fixture
    def api(self, uow, clock):
        sender = RecordingSender()
        state = {"principal": user_principal(Role.ADMIN)}
        app = create_app()
        app.dependency_overrides.update(
            {
                get_principal: lambda: state["principal"],
                get_uow: lambda: uow,
                get_clock: lambda: clock,
                get_webhook_sender: lambda: sender,
            }
        )
        return TestClient(app), sender, state

    BODY = {"url": "https://hooks.example.com/reviewer", "event_types": ["flag_notification"]}

    def test_admin_registers_after_a_successful_ping(self, api, uow, clock):
        client, sender, _ = api
        resp = client.post("/v1/webhooks", json=self.BODY)
        assert resp.status_code == 201
        body = resp.json()
        hook = uow.webhooks.rows[__import__("uuid").UUID(body["webhook_id"])]
        assert body["secret"] == hook.secret and len(hook.secret) >= 40
        assert body["event_types"] == ["flag_notification"] and hook.url == self.BODY["url"]
        assert body["created_at"] == "2026-09-27T20:01:00Z"
        (ping,) = sender.calls
        assert json.loads(ping["body"]) == {
            "notification_type": "verification",
            "webhook_id": body["webhook_id"],
        }
        assert ping["secret"] == hook.secret

    def test_failed_ping_is_422_and_stores_nothing(self, api, uow):
        client, sender, _ = api
        sender.respond = lambda url: SendResult(ok=False, status_code=404, error="HTTP 404")
        resp = client.post("/v1/webhooks", json=self.BODY)
        assert resp.status_code == 422 and "HTTP 404" in resp.json()["detail"]
        assert uow.webhooks.rows == {}

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER])
    def test_non_admin_is_403(self, api, uow, role):
        client, sender, state = api
        state["principal"] = user_principal(role)
        assert client.post("/v1/webhooks", json=self.BODY).status_code == 403
        assert sender.calls == [] and uow.webhooks.rows == {}

    def test_system_key_is_403(self, api):
        client, _, state = api
        state["principal"] = system_principal(__import__("uuid").uuid4())
        assert client.post("/v1/webhooks", json=self.BODY).status_code == 403

    @pytest.mark.parametrize(
        "change",
        [
            {"url": "https://user:pw@hooks.example.com/"},
            {"url": "ftp://hooks.example.com/"},
            {"url": "not a url"},
            {"url": "https://hooks.example.com:70000/"},
            # Regressions found by deep fuzzing (FUZZ_EXAMPLES=1000): non-ASCII and control
            # characters are not schema-valid, so they must be 400, consistently.
            {"url": "https://w/±\u0085g"},
            {"url": "https://h.example/a\u0001b"},
            {"url": "https://h.example:٣/"},  # Arabic-Indic digit as port
            {"event_types": []},
            {"event_types": ["sms"]},
            {"event_types": None},
        ],
    )
    def test_invalid_body_is_400(self, api, change):
        client, sender, _ = api
        body = {k: v for k, v in {**self.BODY, **change}.items() if v is not None}
        assert client.post("/v1/webhooks", json=body).status_code == 400
        assert sender.calls == []

    def test_http_is_refused_in_production(self, api, monkeypatch):
        from app.core.config import AppEnv, Settings
        from app.modules.notifications import router

        client, sender, _ = api
        monkeypatch.setattr(router, "get_settings", lambda: Settings(app_env=AppEnv.PRODUCTION))
        resp = client.post("/v1/webhooks", json={**self.BODY, "url": "http://hooks.example.com/"})
        assert resp.status_code == 400 and sender.calls == []
