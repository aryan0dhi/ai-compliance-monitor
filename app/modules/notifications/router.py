"""POST /v1/webhooks (REQ-04, REQ-11). HTTP mapping only; logic in notifications.service."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.clock import Clock, get_clock
from app.core.config import get_settings
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.notifications.delivery import WebhookSender, sender_from_settings
from app.modules.notifications.schemas import WebhookIn, WebhookOut
from app.modules.notifications.service import (
    InvalidWebhookUrl,
    WebhookAccessDenied,
    WebhookVerificationFailed,
    register_webhook,
)

router = APIRouter(tags=["webhooks"])


def get_webhook_sender() -> WebhookSender:
    """FastAPI dependency: the outbound HTTP client (replaced by a double in tests)."""
    return sender_from_settings()


@router.post(
    "/webhooks",
    status_code=status.HTTP_201_CREATED,
    response_model=WebhookOut,
    responses={
        400: {"description": "Malformed body or disallowed URL"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only admins may register webhooks (D-13)"},
        422: {"description": "The URL did not answer the verification ping with 2xx"},
    },
)
def register_webhook_route(
    body: WebhookIn,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    sender: WebhookSender = Depends(get_webhook_sender),
    clock: Clock = Depends(get_clock),
) -> WebhookOut:
    try:
        registered = register_webhook(
            url=body.url,
            event_types=body.event_types,
            principal=principal,
            uow=uow,
            sender=sender,
            clock=clock,
            require_https=get_settings().webhook_https_required,
        )
    except WebhookAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except InvalidWebhookUrl as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except WebhookVerificationFailed as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"verification ping failed: {exc}"
        ) from exc
    return WebhookOut.from_registered(registered)
