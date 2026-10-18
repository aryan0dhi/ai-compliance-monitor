"""POST /v1/webhooks request and response bodies (Section 5.1)."""

from uuid import UUID

from pydantic import BaseModel, Field

from app.core.types import UtcDatetime
from app.modules.notifications.delivery import MAX_URL_LENGTH, SIGNATURE_HEADER
from app.modules.notifications.models import NotificationType
from app.modules.notifications.service import RegisteredWebhook

# http(s)://host[:port][/path]: no credentials, port 0-65535, path of printable ASCII (RFC 3986;
# anything else must be percent-encoded). Published so clients and fuzzers know the rules;
# production additionally requires https and every target must be public. Explicit ASCII ranges,
# not \d or \s: those are Unicode-aware in the validator but not in every JSON Schema tool.
_PORT = r"(?:6553[0-5]|655[0-2][0-9]|65[0-4][0-9]{2}|6[0-4][0-9]{3}|[1-5][0-9]{4}|[0-9]{1,4})"
WEBHOOK_URL_PATTERN = rf"^https?://[A-Za-z0-9.-]+(?::{_PORT})?(?:/[\x21-\x7e]*)?$"


class WebhookIn(BaseModel):
    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH, pattern=WEBHOOK_URL_PATTERN)
    event_types: list[NotificationType] = Field(min_length=1, max_length=len(NotificationType))


class WebhookOut(BaseModel):
    webhook_id: UUID
    url: str
    event_types: list[NotificationType]
    created_at: UtcDatetime
    secret: str = Field(
        description=f"HMAC-SHA256 key for the {SIGNATURE_HEADER} header "
        "('sha256=' + hex digest of the request body). Shown only once."
    )

    @classmethod
    def from_registered(cls, r: RegisteredWebhook) -> "WebhookOut":
        return cls(
            webhook_id=r.webhook.id,
            url=r.webhook.url,
            event_types=[NotificationType(t) for t in r.webhook.event_types],
            created_at=r.webhook.created_at,
            secret=r.secret,
        )
