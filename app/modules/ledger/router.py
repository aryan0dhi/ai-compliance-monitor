"""GET /v1/ledger/export, GET /v1/ledger/verify, GET /v1/events/{event_id}/audit."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.clock import Clock, get_clock
from app.core.types import INTEGER_QUERY, to_internal_id
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, get_principal
from app.modules.ledger.schemas import (
    AuditEntryOut,
    LedgerExportOut,
    LedgerVerifyOut,
    VerificationRunOut,
)
from app.modules.ledger.service import (
    AuditEventNotFound,
    LedgerAccessDenied,
    VerificationRunDenied,
    event_audit,
    export_ledger,
    list_verifications,
    run_verification,
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


@router.get(
    "/ledger/verifications",
    response_model=list[VerificationRunOut],
    operation_id="listVerifications",
    responses=_READ_ERRORS,
)
def list_verifications_route(
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> list[VerificationRunOut]:
    """Every ledger verification run, scheduled or manual, newest first (contract delta
    \u03945). A bare array, like the audit trail."""
    try:
        checks = list_verifications(principal=principal, uow=uow)
    except LedgerAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return [VerificationRunOut.from_check(c) for c in checks]


@router.post(
    "/ledger/verifications",
    status_code=status.HTTP_201_CREATED,
    response_model=VerificationRunOut,
    operation_id="runVerification",
    responses={
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only reviewer keys start a verification run"},
    },
)
def run_verification_route(
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    clock: Clock = Depends(get_clock),
) -> VerificationRunOut:
    """Verify the whole chain now (trigger MANUAL), as the daily check does (contract delta
    \u03945; REQ-06). A failed run alerts like a scheduled one (D-27)."""
    try:
        check = run_verification(principal=principal, uow=uow, clock=clock)
    except VerificationRunDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    return VerificationRunOut.from_check(check)


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
    parsed_id = to_internal_id(event_id)
    if parsed_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    try:
        entries = event_audit(parsed_id, principal=principal, uow=uow)
    except LedgerAccessDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except AuditEventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    return [AuditEntryOut.from_entry(e) for e in entries]
