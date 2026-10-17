"""GET /v1/events and GET /v1/events/{event_id}. HTTP mapping only; logic in events.service."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.types import IsoAwareDatetime
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.events.models import EventState, EventType
from app.modules.events.repository import EventFilters
from app.modules.events.schemas import EventDetail, EventPageOut
from app.modules.events.service import EventAccessDenied, EventNotFound, get_event, list_events
from app.modules.ledger.interface import LedgerWriter, get_ledger

router = APIRouter(prefix="/events", tags=["events"])

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
# PostgreSQL OFFSET is a bigint; larger values were a 500 (found by fuzzing on PostgreSQL).
MAX_OFFSET = 2**63 - 1


@router.get(
    "",
    response_model=EventPageOut,
    responses={
        400: {"description": "Invalid query parameter"},
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Role may not read events (D-06)"},
    },
)
def list_events_route(
    system_id: UUID | None = None,
    state: EventState | None = None,
    event_type: EventType | None = Query(None, alias="type"),
    since: IsoAwareDatetime | None = Query(None, description="Only events detected at or after"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0, le=MAX_OFFSET),
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> EventPageOut:
    filters = EventFilters(system_id=system_id, state=state, type=event_type, since=since)
    try:
        page = list_events(filters, limit=limit, offset=offset, principal=principal, uow=uow)
    except EventAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return EventPageOut.from_page(page)


@router.get(
    "/{event_id}",
    response_model=EventDetail,
    responses={
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Role may not read events (D-06)"},
        404: {"description": "Unknown event_id"},
    },
)
def get_event_route(
    event_id: str,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
) -> EventDetail:
    # A malformed id can't name an existing event, so it is a 404 like any unknown id (D-06).
    try:
        parsed_id = UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    try:
        event = get_event(parsed_id, principal=principal, uow=uow, ledger=ledger)
    except EventAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    return EventDetail.from_event(event)
