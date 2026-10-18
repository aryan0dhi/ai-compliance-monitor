"""Outbound webhook requests: SSRF guard, address pinning and HMAC signatures (REQ-04).

Webhook URLs are supplied by API callers, so every request:
- accepts only http(s) URLs without embedded credentials (https only in production);
- resolves the host and refuses it if ANY address is private, loopback, link-local, multicast or
  otherwise non-global (unless WEBHOOK_ALLOW_PRIVATE_NETWORKS is set outside production);
- connects to the vetted address itself, with the original Host header and TLS server name, so a
  DNS answer that changes between the check and the connection (rebinding) can't redirect it;
- never follows redirects, ignores proxy environment variables, and doesn't read the response
  body (only the status code matters).
"""

import hashlib
import hmac
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import httpx

MAX_URL_LENGTH = 2048
# Addresses of one host tried per attempt (each can take up to WEBHOOK_TIMEOUT_S to fail).
MAX_ADDRESSES_TRIED = 3
SIGNATURE_HEADER = "X-Signature-SHA256"
USER_AGENT = "ai-compliance-monitor/0.1"

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class UnsafeWebhookUrl(ValueError):
    """The URL may not be used as a webhook target."""


@dataclass(frozen=True)
class WebhookTarget:
    scheme: str
    host: str
    port: int
    path_and_query: str

    @property
    def default_port(self) -> bool:
        return self.port == (443 if self.scheme == "https" else 80)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    status_code: int | None
    error: str | None


class WebhookSender(Protocol):
    def send(self, url: str, body: bytes, secret: str) -> SendResult: ...


def parse_webhook_url(url: str, *, require_https: bool) -> WebhookTarget:
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeWebhookUrl(f"URL longer than {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeWebhookUrl("not a valid URL") from exc
    allowed = ("https",) if require_https else ("https", "http")
    if parts.scheme not in allowed:
        raise UnsafeWebhookUrl(f"scheme must be {' or '.join(allowed)}")
    if parts.username is not None or parts.password is not None:
        raise UnsafeWebhookUrl("URL must not contain credentials")
    if not parts.hostname:
        raise UnsafeWebhookUrl("URL has no host")
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return WebhookTarget(
        scheme=parts.scheme,
        host=parts.hostname,
        port=port or (443 if parts.scheme == "https" else 80),
        path_and_query=path,
    )


def is_public_address(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def resolve_addresses(target: WebhookTarget, *, allow_private: bool) -> list[IPAddress]:
    """The host's addresses, refusing the target if any of them isn't public."""
    try:
        infos = socket.getaddrinfo(target.host, target.port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise UnsafeWebhookUrl("host could not be resolved") from exc
    addresses: list[IPAddress] = []
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise UnsafeWebhookUrl("host could not be resolved")
    if not allow_private and not all(is_public_address(a) for a in addresses):
        raise UnsafeWebhookUrl("host resolves to a private or reserved address")
    return addresses


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


Resolver = Callable[..., list[IPAddress]]


class HttpWebhookSender:
    def __init__(
        self,
        *,
        timeout_s: float,
        allow_private: bool,
        require_https: bool,
        resolver: Resolver = resolve_addresses,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = httpx.Timeout(timeout_s)
        self._allow_private = allow_private
        self._require_https = require_https
        self._resolver = resolver
        self._transport = transport

    def send(self, url: str, body: bytes, secret: str) -> SendResult:
        try:
            target = parse_webhook_url(url, require_https=self._require_https)
            addresses = self._resolver(target, allow_private=self._allow_private)
        except UnsafeWebhookUrl as exc:
            return SendResult(ok=False, status_code=None, error=str(exc))

        host = f"[{target.host}]" if ":" in target.host else target.host
        headers = {
            "Host": host if target.default_port else f"{host}:{target.port}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            SIGNATURE_HEADER: sign(body, secret),
        }
        extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
        result = SendResult(ok=False, status_code=None, error="no address to connect to")
        # Try each vetted address until one accepts the connection (e.g. a dual-stack host whose
        # IPv6 address is unreachable from the container). Anything after connecting is final, so
        # a request is never sent twice in one attempt.
        for address in addresses[:MAX_ADDRESSES_TRIED]:
            ip_host = f"[{address}]" if address.version == 6 else str(address)
            pinned_url = f"{target.scheme}://{ip_host}:{target.port}{target.path_and_query}"
            try:
                with (
                    httpx.Client(
                        timeout=self._timeout,
                        follow_redirects=False,
                        trust_env=False,
                        transport=self._transport,
                    ) as client,
                    client.stream(
                        "POST", pinned_url, content=body, headers=headers, extensions=extensions
                    ) as response,
                ):
                    status_code = response.status_code
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                result = SendResult(False, None, f"{type(exc).__name__}: {exc}"[:500])
                continue
            except httpx.HTTPError as exc:
                return SendResult(False, None, f"{type(exc).__name__}: {exc}"[:500])
            if 200 <= status_code < 300:
                return SendResult(ok=True, status_code=status_code, error=None)
            return SendResult(ok=False, status_code=status_code, error=f"HTTP {status_code}")
        return result


def sender_from_settings() -> HttpWebhookSender:
    from app.core.config import get_settings

    settings = get_settings()
    return HttpWebhookSender(
        timeout_s=settings.webhook_timeout_s,
        allow_private=settings.webhook_private_networks_allowed,
        require_https=settings.webhook_https_required,
    )
