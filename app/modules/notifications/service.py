"""Webhook registration (POST /v1/webhooks, Section 5.1)."""

import hashlib
import json
import secrets
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.principal import Principal, Role
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.notifications.delivery import SendResult, WebhookSender, parse_webhook_url
from app.modules.notifications.models import NotificationType, Webhook

# Registering a URL that will receive event data is a configuration change (D-13).
WEBHOOK_ADMIN_ROLES = frozenset({Role.ADMIN})


class WebhookAccessDenied(Exception):
    pass


class InvalidWebhookUrl(ValueError):
    """Malformed or disallowed URL (400)."""


class WebhookVerificationFailed(Exception):
    """The verification ping did not get a 2xx response (422)."""


@dataclass(frozen=True)
class RegisteredWebhook:
    webhook: Webhook
    # Shown once: the receiver uses it to check the X-Signature-SHA256 header.
    secret: str


def verification_body(webhook_id: UUID) -> bytes:
    return json.dumps(
        {"notification_type": "verification", "webhook_id": str(webhook_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def register_webhook(
    *,
    url: str,
    event_types: list[NotificationType],
    principal: Principal,
    uow: UnitOfWork,
    sender: WebhookSender,
    clock: Clock,
    require_https: bool,
    ledger: LedgerWriter,
) -> RegisteredWebhook:
    if principal.role not in WEBHOOK_ADMIN_ROLES:
        raise WebhookAccessDenied(f"role {principal.role} may not register webhooks")
    try:
        parse_webhook_url(url, require_https=require_https)
    except ValueError as exc:
        raise InvalidWebhookUrl(str(exc)) from exc

    webhook_id, secret = uuid4(), secrets.token_urlsafe(32)
    # The ping runs before any transaction is opened; the sender re-checks the resolved address.
    ping: SendResult = sender.send(url, verification_body(webhook_id), secret)
    if not ping.ok:
        raise WebhookVerificationFailed(ping.error or "verification ping failed")

    webhook = Webhook(
        id=webhook_id,
        url=url,
        event_types=sorted({t.value for t in event_types}),
        secret=secret,
        created_by=principal.actor_id,
        created_at=clock.now(),
        disabled_at=None,
    )
    with uow:
        uow.webhooks.add(webhook)
        # Contract delta \u03948 (D-29): the registration is audited, with the URL's SHA-256 only;
        # a URL can carry tokens or point inside a network, so it never enters the ledger.
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.WEBHOOK_REGISTERED,
            actor_id=principal.actor_id,
            payload={
                "webhook_id": str(webhook.id),
                "url_sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                "event_types": webhook.event_types,
            },
        )
    return RegisteredWebhook(webhook=webhook, secret=secret)
