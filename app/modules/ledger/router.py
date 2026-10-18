"""GET /v1/ledger/export, GET /v1/ledger/verify, GET /v1/events/{event_id}/audit."""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.types import INTEGER_QUERY
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.ledger.schemas import AuditEntryOut, LedgerExportOut, LedgerVerifyOut
from app.modules.ledger.service import (
    AuditEventNotFound,
    LedgerAccessDenied,
    event_audit,
    export_ledger,
    verify_ledger,
)

router = APIRouter(tags=["ledger"])

MAX_EXPORT_PAGE = 10_000
MAX_SEQ = 2**63 - 1  # ledger_entry.seq is a bigint

_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "Role may not read the ledger"},
}


@router.get(
    "/ledger/export",
    response_model=LedgerExportOut,
    responses={**_READ_ERRORS, 400: {"description": "Invalid query parameter"}},
)
def export_route(
    after_seq: Annotated[
        int, Query(ge=0, le=MAX_SEQ, description="Return entries after this seq"), INTEGER_QUERY
    ] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_PAGE), INTEGER_QUERY] = 1000,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> LedgerExportOut:
    try:
        page = export_ledger(after_seq=after_seq, limit=limit, principal=principal, uow=uow)
    except LedgerAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return LedgerExportOut.from_page(page)


@router.get("/ledger/verify", response_model=LedgerVerifyOut, responses=_READ_ERRORS)
def verify_route(
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> LedgerVerifyOut:
    try:
        result = verify_ledger(principal=principal, uow=uow)
    except LedgerAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return LedgerVerifyOut.from_result(result)


@router.get(
    "/events/{event_id}/audit",
    response_model=list[AuditEntryOut],
    responses={**_READ_ERRORS, 404: {"description": "Unknown event_id"}},
    tags=["events"],
)
def audit_route(
    event_id: str,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> list[AuditEntryOut]:
    try:
        parsed_id = UUID(event_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    try:
        entries = event_audit(parsed_id, principal=principal, uow=uow)
    except LedgerAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuditEventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    return [AuditEntryOut.from_entry(e) for e in entries]
