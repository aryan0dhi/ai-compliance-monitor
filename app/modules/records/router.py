"""POST /v1/events/{event_id}/erasure (contract delta Δ6). HTTP mapping only; logic in
records.service."""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.core.clock import Clock, get_clock
from app.core.errors import http_error
from app.core.types import IdOut, UtcDatetime, to_internal_id
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.ledger.interface import LedgerWriter, get_ledger
from app.modules.records.service import (
    AlreadyErased,
    ErasureDenied,
    RetentionNotExpired,
    UnknownEvent,
    authorize_erasure,
    erase_on_request,
)

router = APIRouter(prefix="/events", tags=["records"])


class ErasureIn(BaseModel):
    """The body is empty or `{}` (Δ6); anything else is 400 MALFORMED_BODY."""

    model_config = ConfigDict(extra="forbid")


class ErasureOut(BaseModel):
    event_id: IdOut
    erased_at: UtcDatetime
    # Which personal-data fields held anything and are now unreadable (D-29).
    erased_fields: list[str]


@router.post(
    "/{event_id}/erasure",
    response_model=ErasureOut,
    operation_id="eraseRecord",
    responses={
        400: {"description": "MALFORMED_BODY: a body other than empty or {}"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only reviewer keys erase records"},
        404: {"description": "Unknown event_id"},
        409: {"description": "RETENTION_NOT_EXPIRED before retain_until, or ALREADY_ERASED"},
    },
)
def erase_route(
    event_id: str,
    body: ErasureIn | None = None,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    clock: Clock = Depends(get_clock),
) -> ErasureOut:
    """Erase the event's personal data now (REQ-16): its record key is destroyed with one
    RECORD_ERASED ledger entry. Allowed once the event's retain_until has passed (REQ-05)."""
    try:
        # The role first, so a non-reviewer gets 403 for any id.
        authorize_erasure(principal)
        parsed_id = to_internal_id(event_id)
        if parsed_id is None:
            raise UnknownEvent(event_id)
        erasure = erase_on_request(
            parsed_id, principal=principal, uow=uow, ledger=ledger, clock=clock
        )
    except ErasureDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except UnknownEvent as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    except (AlreadyErased, RetentionNotExpired) as exc:
        raise http_error(status.HTTP_409_CONFLICT, exc) from exc
    return ErasureOut(
        event_id=erasure.event_id,
        erased_at=erasure.erased_at,
        erased_fields=erasure.erased_fields,
    )
