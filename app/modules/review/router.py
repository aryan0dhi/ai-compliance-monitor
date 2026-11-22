"""POST /v1/events/{event_id}/disposition, GET /v1/events/{event_id}/report and
POST /v1/events/{event_id}/report/submission. HTTP mapping only; logic in review.disposition and
review.report."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.clock import Clock, get_clock
from app.core.errors import http_error
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
from app.modules.review.refusals import record_refusal
from app.modules.review.report import (
    ReportDenied,
    ReportUnavailable,
    authorize_report,
    get_report,
    mark_report_submitted,
)
from app.modules.review.report_schemas import ReportOut, ReportSubmissionIn, report_body
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
            "description": "A harm field is still PENDING (B-08), a revision to a less strict "
            "tier has no rationale (B-10), or the event is a monitoring gap (INV-12)"
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

    A repeat call appends a revision; nothing is overwritten (B-09). deadline_utc follows B-10.
    """
    parsed_id = to_internal_id(event_id)
    try:
        # The role first, so a non-reviewer gets 403 for any id.
        authorize_disposition(principal)
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
        raise _refused(exc, 403, "recordDisposition", parsed_id, principal, uow, ledger) from exc
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    except MissingRationale as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except DispositionConflict as exc:
        raise _refused(exc, 409, "recordDisposition", parsed_id, principal, uow, ledger) from exc
    return EventDetail.from_record(record)


def _refused(
    exc: Exception,
    status_code: int,
    operation: str,
    event_id: UUID | None,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
) -> StarletteHTTPException:
    """The HTTP error for a refused call, after recording the attempt in the ledger (D-26)."""
    record_refusal(
        event_id,
        operation=operation,
        status_code=status_code,
        reason=str(exc),
        principal=principal,
        uow=uow,
        ledger=ledger,
    )
    return http_error(status_code, exc)


_REPORT_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "Only reviewer keys read or submit reports (REQ-13)"},
    404: {"description": "Unknown event_id"},
}


def _report_errors(
    exc: Exception,
    operation: str,
    event_id: UUID | None,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
) -> StarletteHTTPException:
    if isinstance(exc, EventNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "event not found")
    code = 403 if isinstance(exc, ReportDenied) else 409
    return _refused(exc, code, operation, event_id, principal, uow, ledger)


@router.get(
    "/{event_id}/report",
    response_model=ReportOut,
    operation_id="getReport",
    tags=["reports"],
    responses={
        **_REPORT_RESPONSES,
        409: {
            "description": "The event's current tier isn't serious, it has no disposition yet "
            "(REQ-08a), or it is a monitoring gap (INV-12)"
        },
    },
)
def get_report_route(
    event_id: str,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Submission-ready Article 73 report of the current determination (REQ-12, B-12).

    Assembled on the first call after a serious-tier disposition; every later call returns the
    same bytes until a revision or the submission mark (D-25).
    """
    parsed_id = to_internal_id(event_id)
    try:
        authorize_report(principal)
        if parsed_id is None:
            raise EventNotFound(event_id)
        report = get_report(parsed_id, principal=principal, uow=uow, ledger=ledger, clock=clock)
    except (ReportDenied, EventNotFound, ReportUnavailable) as exc:
        raise _report_errors(exc, "getReport", parsed_id, principal, uow, ledger) from exc
    return Response(report_body(report), media_type="application/json")


@router.post(
    "/{event_id}/report/submission",
    response_model=ReportOut,
    operation_id="markReportSubmitted",
    tags=["reports"],
    responses={
        **_REPORT_RESPONSES,
        400: {"description": "Malformed body (note longer than 2000 characters)"},
        409: {
            "description": "No report of the current serious-tier determination has been "
            "assembled yet, or it is already marked submitted"
        },
    },
)
def mark_report_submitted_route(
    event_id: str,
    body: ReportSubmissionIn | None = None,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    clock: Clock = Depends(get_clock),
) -> Response:
    """Record that the reviewer submitted the report to the authority, which happens outside
    the system. Returns the report with submitted_at set."""
    parsed_id = to_internal_id(event_id)
    try:
        authorize_report(principal)
        if parsed_id is None:
            raise EventNotFound(event_id)
        report = mark_report_submitted(
            parsed_id,
            body.note if body is not None else None,
            principal=principal,
            uow=uow,
            ledger=ledger,
            clock=clock,
        )
    except (ReportDenied, EventNotFound, ReportUnavailable) as exc:
        raise _report_errors(exc, "markReportSubmitted", parsed_id, principal, uow, ledger) from exc
    return Response(report_body(report), media_type="application/json")
