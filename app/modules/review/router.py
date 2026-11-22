"""POST /v1/events/{event_id}/disposition, GET /v1/events/{event_id}/report,
POST /v1/events/{event_id}/report/submission, GET /v1/events/{event_id}/reports and
GET /v1/reports/{report_id}. HTTP mapping only; logic in review.disposition and
review.report."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
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
    RecordErased,
    ReportDenied,
    ReportNotFound,
    ReportUnavailable,
    SubmissionRequest,
    authorize_report,
    get_report,
    get_report_version,
    list_report_versions,
    submit_report,
)
from app.modules.review.report_schemas import (
    ReportOut,
    ReportSubmissionIn,
    ReportVersionOut,
    RevealedReport,
    SubmissionOut,
    report_body,
)
from app.modules.review.schemas import DispositionIn

router = APIRouter(prefix="/events", tags=["review"])
# GET /v1/reports/{report_id} (contract delta \u03944).
reports_router = APIRouter(prefix="/reports", tags=["reports"])


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
    if isinstance(exc, ReportNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    if isinstance(exc, RecordErased):
        return http_error(status.HTTP_410_GONE, exc)
    code = 403 if isinstance(exc, ReportDenied) else 409
    return _refused(exc, code, operation, event_id, principal, uow, ledger)


def report_headers(report: RevealedReport) -> dict[str, str]:
    """Contract delta \u03942: which stored version this is, and its hash."""
    return {
        "ETag": f'"{report.report_sha256}"',
        "X-Report-Id": str(report.report_id),
        "X-Report-Version": str(report.disposition_seq),
        "X-Report-Sha256": report.report_sha256,
    }


@router.get(
    "/{event_id}/report",
    response_model=ReportOut,
    operation_id="getReport",
    tags=["reports"],
    responses={
        **_REPORT_RESPONSES,
        400: {"description": "MALFORMED_QUERY: format is not json or pdf"},
        409: {
            "description": "NO_DISPOSITION (REQ-08a), the event's current tier isn't serious, "
            "or it is a monitoring gap (INV-12)"
        },
        410: {"description": "RECORD_ERASED: the event's personal data was erased"},
        501: {"description": "format=pdf isn't implemented yet"},
    },
)
def get_report_route(
    event_id: str,
    report_format: Annotated[
        Literal["json", "pdf"],
        Query(alias="format", description="json (default) or pdf (contract delta \u03942)"),
    ] = "json",
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
        if report_format == "pdf":
            # The PDF layout comes from the delta's report.schema.json, which we don't have.
            raise HTTPException(
                status.HTTP_501_NOT_IMPLEMENTED,
                "PDF reports are not implemented yet (contract delta \u03942); use format=json",
            )
        report = get_report(parsed_id, principal=principal, uow=uow, ledger=ledger, clock=clock)
    except (ReportDenied, EventNotFound, ReportUnavailable, RecordErased) as exc:
        raise _report_errors(exc, "getReport", parsed_id, principal, uow, ledger) from exc
    return Response(
        report_body(report), media_type="application/json", headers=report_headers(report)
    )


@router.post(
    "/{event_id}/report/submission",
    status_code=status.HTTP_201_CREATED,
    response_model=SubmissionOut,
    operation_id="submitReport",
    tags=["reports"],
    responses={
        **_REPORT_RESPONSES,
        400: {"description": "MALFORMED_BODY"},
        404: {"description": "Unknown event_id, or a report_id that isn't one of its versions"},
        409: {
            "description": "NOT_SUBMITTABLE, REPORT_NOT_CURRENT, REPORT_HASH_MISMATCH or "
            "REPORT_ALREADY_SUBMITTED"
        },
        410: {"description": "RECORD_ERASED: the event's personal data was erased"},
    },
)
def submit_report_route(
    event_id: str,
    body: ReportSubmissionIn,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
    clock: Clock = Depends(get_clock),
) -> SubmissionOut:
    """Record that the reviewer submitted a report version to the authority (contract delta
    \u03943). Nothing is sent: the submission itself happens outside the system."""
    parsed_id = to_internal_id(event_id)
    try:
        authorize_report(principal)
        if parsed_id is None:
            raise EventNotFound(event_id)
        report_id = to_internal_id(body.report_id)
        if report_id is None:
            raise ReportNotFound(body.report_id)
        submission = submit_report(
            parsed_id,
            SubmissionRequest(
                report_id=report_id,
                json_sha256=body.json_sha256,
                report_type=body.report_type,
                expected_next_report_date=body.expected_next_report_date,
                authority_reference=body.authority_reference,
            ),
            principal=principal,
            uow=uow,
            ledger=ledger,
            clock=clock,
        )
    except (ReportDenied, EventNotFound, ReportNotFound, RecordErased, ReportUnavailable) as exc:
        raise _report_errors(exc, "submitReport", parsed_id, principal, uow, ledger) from exc
    return SubmissionOut.from_submission(submission)


@router.get(
    "/{event_id}/reports",
    response_model=list[ReportVersionOut],
    operation_id="listReportVersions",
    tags=["reports"],
    responses={
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only reviewer and viewer keys list report versions"},
        404: {"description": "Unknown event_id"},
    },
)
def list_report_versions_route(
    event_id: str,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
) -> list[ReportVersionOut]:
    """Every stored report version of the event, oldest first, with its status (contract delta
    \u03944). A bare array, like the audit trail."""
    try:
        parsed_id = to_internal_id(event_id)
        if parsed_id is None:
            raise EventNotFound(event_id)
        versions = list_report_versions(parsed_id, principal=principal, uow=uow)
    except ReportDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except EventNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "event not found") from exc
    return [
        ReportVersionOut(
            report_id=v.report.id,
            event_id=v.report.event_id,
            report_version=v.report.disposition_seq,
            status=v.status.value,
            assembled_at=v.report.assembled_at,
            report_sha256=v.report.report_sha256,
            submitted_at=v.report.submitted_at,
        )
        for v in versions
    ]


@reports_router.get(
    "/{report_id}",
    response_model=ReportOut,
    operation_id="getReportVersion",
    responses={
        401: {"description": "Missing or invalid API key"},
        403: {"description": "Only reviewer keys read reports (REQ-13)"},
        404: {"description": "Unknown report_id"},
        410: {"description": "RECORD_ERASED: the event's personal data was erased"},
    },
)
def get_report_version_route(
    report_id: str,
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    ledger: LedgerWriter = Depends(get_ledger),
) -> Response:
    """One stored report version, byte for byte as assembled (contract delta \u03944)."""
    try:
        authorize_report(principal)
        parsed_id = to_internal_id(report_id)
        if parsed_id is None:
            raise ReportNotFound(report_id)
        report = get_report_version(parsed_id, principal=principal, uow=uow, ledger=ledger)
    except ReportDenied as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    except ReportNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found") from exc
    except RecordErased as exc:
        raise http_error(status.HTTP_410_GONE, exc) from exc
    return Response(
        report_body(report), media_type="application/json", headers=report_headers(report)
    )
