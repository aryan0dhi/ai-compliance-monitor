"""POST /v1/events/{event_id}/disposition. HTTP mapping only; logic in review.disposition."""

from fastapi import APIRouter, Depends, HTTPException, status

from app.core.clock import Clock, get_clock
from app.core.types import to_internal_id
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.events.schemas import EventDetail
from app.modules.events.service import EventNotFound
from app.modules.ledger.interface import LedgerWriter, get_ledger
from app.modules.review.disposition import (
    DispositionConflict,
    DispositionDenied,
    MissingRationale,
    authorize_disposition,
    record_disposition,
)
from app.modules.review.schemas import DispositionIn

router = APIRouter(prefix="/events", tags=["review"])


@router.post(
    "/{event_id}/disposition",
    response_model=EventDetail,
    operation_id="recordDisposition",
    responses={
        400: {"description": "Malformed body, or NOT_SERIOUS without a rationale (B-08)"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only reviewer keys record dispositions (REQ-13)"},
        404: {"description": "Unknown event_id"},
        409: {
            "description": "A harm field is still PENDING (B-08), or the event is a monitoring "
            "gap (INV-12)"
        },
    },
)
def record_disposition_route(
    event_id: str,
    body: DispositionIn,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    clock: Clock = Depends(get_clock),
) -> EventDetail:
    """Confirm the Art. 3(49) fields and assign or revise the severity tier (REQ-08a/b, REQ-09).

    A repeat call appends a revision; nothing is overwritten (B-09).
    """
    try:
        # The role first, so a non-reviewer gets 403 for any id.
        authorize_disposition(principal)
        parsed_id = to_internal_id(event_id)
        if parsed_id is None:
            raise EventNotFound(event_id)
        record = record_disposition(
            parsed_id,
            body.to_request(),
            principal=principal,
            uow=uow,
            ledger=ledger,
            clock=clock,
        )
    except DispositionDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    except MissingRationale as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except DispositionConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return EventDetail.from_record(record)
