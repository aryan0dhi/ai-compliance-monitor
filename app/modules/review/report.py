"""Article 73 reports: GET /v1/events/{id}/report and POST /v1/events/{id}/report/submission
(REQ-08a, REQ-12, REQ-13; pod rule B-12; D-25).

A report exists once the event's current disposition is a serious tier. The first request for it
assembles the document from the event, its harm fields and that disposition, stores the JSON
text, and writes one REPORT_ASSEMBLED ledger entry; later requests return the stored text and
write one VIEW entry each (B-14). A revision gets a report of its own on its next request; the
report of an earlier determination is kept but no longer served.

The event row is locked while a report is read or changed, as when a disposition is recorded, so
a report is never assembled from a determination that is being replaced, and two first requests
can't both assemble one.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from app.core.clock import Clock
from app.core.errors import ErrorCode
from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event, EventType
from app.modules.events.service import EventNotFound, EventRecord
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.records.service import (
    Vault,
    note_purpose,
    open_vault,
    report_purpose,
    revealed_evidence,
    revealed_rationales,
)
from app.modules.review.models import (
    Disposition,
    DispositionTier,
    Report,
    ReportSubmission,
    ReportType,
    SubmissionKind,
)
from app.modules.review.report_schemas import RevealedReport, assemble_document, sha256_hex

# B-13 / REQ-13: reports are for reviewers; viewers and admins get 403 like for dispositions.
REPORT_ROLES = frozenset({Role.REVIEWER})


class ReportDenied(Exception):
    """The caller's role may not read or submit reports (403)."""


class ReportUnavailable(Exception):
    """The event has no report in its current state, or it can't be marked submitted (409)."""


class NoDisposition(ReportUnavailable):
    """The event has no disposition yet (409 NO_DISPOSITION, REQ-08a; contract delta \u03942)."""

    error_code = ErrorCode.NO_DISPOSITION


class ReportNotFound(Exception):
    """No report version with this id (404)."""


class RecordErased(Exception):
    """The event's personal data, and so its reports, were erased (410 RECORD_ERASED)."""

    error_code = ErrorCode.RECORD_ERASED

    def __init__(self) -> None:
        super().__init__("the personal data of this event has been erased")


class NotSubmittable(ReportUnavailable):
    """The event has no serious-tier determination to report (409 NOT_SUBMITTABLE)."""

    error_code = ErrorCode.NOT_SUBMITTABLE


class ReportNotCurrent(ReportUnavailable):
    """The report version isn't the event's current one (409 REPORT_NOT_CURRENT)."""

    error_code = ErrorCode.REPORT_NOT_CURRENT


class ReportHashMismatch(ReportUnavailable):
    """json_sha256 isn't the stored report's hash (409 REPORT_HASH_MISMATCH)."""

    error_code = ErrorCode.REPORT_HASH_MISMATCH


class ReportAlreadySubmitted(ReportUnavailable):
    """This report version has a submission already (409 REPORT_ALREADY_SUBMITTED)."""

    error_code = ErrorCode.REPORT_ALREADY_SUBMITTED


# Contract delta \u03944: the version list is for reviewers and viewers; report content and
# submissions stay reviewer-only (REPORT_ROLES).
REPORT_LIST_ROLES = frozenset({Role.REVIEWER, Role.VIEWER})


def authorize_report(principal: Principal) -> None:
    if principal.role not in REPORT_ROLES:
        raise ReportDenied(f"role {principal.role} may not access reports")


def _locked_event(uow: UnitOfWork, event_id: UUID) -> Event:
    event = uow.events.get_for_update(event_id)
    if event is None:
        raise EventNotFound(str(event_id))
    return event


def _serious_history(uow: UnitOfWork, event: Event) -> list[Disposition]:
    """The event's dispositions, whose last is a serious tier; else ReportUnavailable."""
    if event.type != EventType.THRESHOLD_BREACH:
        raise ReportUnavailable("monitoring-gap events have no report (INV-12)")
    history = uow.dispositions.for_event(event.id)
    if not history:
        raise NoDisposition("the event has no disposition yet (REQ-08a)")
    if not DispositionTier(history[-1].tier).is_serious:
        raise ReportUnavailable("the event's current tier is NOT_SERIOUS, so it has no report")
    return history


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _open_vault(uow: UnitOfWork, event: Event, now: datetime) -> Vault:
    vault = open_vault(uow, event.id, create_at=now)
    if vault.erased_at is not None:
        # Contract delta \u03942: 410 RECORD_ERASED.
        raise RecordErased()
    return vault


def _revealed(report: Report, vault: Vault) -> RevealedReport:
    document = vault.reveal(report_purpose(report.disposition_seq), report.document)
    if document is None:  # pragma: no cover - _open_vault refuses erased events first
        raise ReportUnavailable("the report was erased (REQ-16)")
    return RevealedReport(
        report_id=report.id,
        event_id=report.event_id,
        disposition_seq=report.disposition_seq,
        document=document,
        report_sha256=report.report_sha256,
        assembled_at=report.assembled_at,
        assembled_by=report.assembled_by,
        submitted_at=report.submitted_at,
        submitted_by=report.submitted_by,
        submission_note=vault.reveal(note_purpose(report.disposition_seq), report.submission_note),
    )


def get_report(
    event_id: UUID,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> RevealedReport:
    """The report of the event's current determination, assembling and storing it first if
    needed. Raises ReportDenied (403), EventNotFound (404) or ReportUnavailable (409); nothing is
    written in those cases.

    The stored document is sealed with the event's key (REQ-16); report_sha256 is over the
    document as assembled, so the hash identifies the report even after the key is destroyed."""
    authorize_report(principal)
    with uow:
        event = _locked_event(uow, event_id)
        history = _serious_history(uow, event)
        vault = _open_vault(uow, event, clock.now())
        current = history[-1]
        report = uow.reports.get(event.id, current.seq)
        if report is not None:
            ledger.append(
                uow.session,
                entry_type=LedgerEntryType.VIEW,
                actor_id=principal.actor_id,
                payload={"event_id": str(event.id), "report_sha256": report.report_sha256},
                event_id=event.id,
            )
            return _revealed(report, vault)
        now = clock.now()
        harm_fields = uow.harm_fields.for_event(event.id)
        record = EventRecord(
            event=event,
            harm_fields=harm_fields,
            dispositions=history,
            rationales=revealed_rationales(vault, history),
            evidence=revealed_evidence(vault, harm_fields),
        )
        document = assemble_document(record, assembled_at=now)
        sealed = vault.seal(report_purpose(current.seq), document)
        if sealed is None:  # pragma: no cover - a document is never None
            raise ValueError("an assembled report has text")
        report = Report(
            id=uuid4(),
            event_id=event.id,
            disposition_seq=current.seq,
            document=sealed,
            report_sha256=sha256_hex(document),
            assembled_at=now,
            assembled_by=principal.actor_id,
        )
        uow.reports.add(report)
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.REPORT_ASSEMBLED,
            actor_id=principal.actor_id,
            payload={
                "event_id": str(event.id),
                "disposition_seq": current.seq,
                "tier": current.tier,
                "assembled_at": _iso(now),
                "report_sha256": report.report_sha256,
            },
            event_id=event.id,
        )
    return _revealed(report, vault)


# --- report versions and submissions (contract delta \u03943, \u03944; D-29) ------------------


class ReportStatus(StrEnum):
    ASSEMBLED = "ASSEMBLED"
    SUBMITTED = "SUBMITTED"
    SUPERSEDED = "SUPERSEDED"
    SUBMITTED_SUPERSEDED = "SUBMITTED_SUPERSEDED"


@dataclass(frozen=True)
class ReportVersion:
    report: Report
    status: ReportStatus


def _current_seq(history: list[Disposition]) -> int | None:
    """The disposition a current report is assembled from: the latest one, if serious."""
    if history and DispositionTier(history[-1].tier).is_serious:
        return history[-1].seq
    return None


def _status(report: Report, current_seq: int | None, submitted: set[UUID]) -> ReportStatus:
    was_submitted = report.id in submitted or report.submitted_at is not None
    if report.disposition_seq == current_seq:
        return ReportStatus.SUBMITTED if was_submitted else ReportStatus.ASSEMBLED
    return ReportStatus.SUBMITTED_SUPERSEDED if was_submitted else ReportStatus.SUPERSEDED


def list_report_versions(
    event_id: UUID, *, principal: Principal, uow: UnitOfWork
) -> list[ReportVersion]:
    """GET /v1/events/{id}/reports: every stored version, oldest first, with its status."""
    if principal.role not in REPORT_LIST_ROLES:
        raise ReportDenied(f"role {principal.role} may not list reports")
    with uow:
        event = uow.events.get(event_id)
        if event is None:
            raise EventNotFound(str(event_id))
        current = _current_seq(uow.dispositions.for_event(event_id))
        submitted = {s.report_id for s in uow.report_submissions.for_event(event_id)}
        return [
            ReportVersion(report=r, status=_status(r, current, submitted))
            for r in uow.reports.for_event(event_id)
        ]


def get_report_version(
    report_id: UUID,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
) -> RevealedReport:
    """GET /v1/reports/{report_id}: one stored version, exactly as assembled. Reviewer only;
    404 unknown, 410 once the event's personal data was erased. One VIEW entry (B-14)."""
    authorize_report(principal)
    with uow:
        report = uow.reports.by_id(report_id)
        if report is None:
            raise ReportNotFound(str(report_id))
        vault = open_vault(uow, report.event_id)
        if vault.erased_at is not None:
            raise RecordErased()
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.VIEW,
            actor_id=principal.actor_id,
            payload={
                "event_id": str(report.event_id),
                "report_id": str(report.id),
                "report_sha256": report.report_sha256,
            },
            event_id=report.event_id,
        )
        return _revealed(report, vault)


@dataclass(frozen=True)
class SubmissionRequest:
    report_id: UUID
    json_sha256: str
    report_type: ReportType
    expected_next_report_date: date | None = None
    authority_reference: str | None = None


def submit_report(
    event_id: UUID,
    request: SubmissionRequest,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> ReportSubmission:
    """POST /v1/events/{id}/report/submission (contract delta \u03943): record that the reviewer
    submitted a report version to the authority; nothing is sent. The first submission of an
    event is INITIAL, later ones FOLLOW_UP. Checks, in order: 403, 404 (event, or a report_id
    that isn't one of its versions), 410 RECORD_ERASED, 409 NOT_SUBMITTABLE, REPORT_NOT_CURRENT,
    REPORT_HASH_MISMATCH, REPORT_ALREADY_SUBMITTED. Nothing is written on any of them."""
    authorize_report(principal)
    with uow:
        event = _locked_event(uow, event_id)
        report = uow.reports.by_id(request.report_id)
        if report is None or report.event_id != event.id:
            raise ReportNotFound(str(request.report_id))
        if open_vault(uow, event.id).erased_at is not None:
            raise RecordErased()
        history = uow.dispositions.for_event(event.id)
        current = _current_seq(history)
        if event.type != EventType.THRESHOLD_BREACH or current is None:
            raise NotSubmittable("the event has no serious-tier determination to report")
        if report.disposition_seq != current:
            raise ReportNotCurrent("this report version has been superseded by a revision")
        if request.json_sha256 != report.report_sha256:
            raise ReportHashMismatch("json_sha256 is not the hash of this report version")
        earlier = uow.report_submissions.for_event(event.id)
        if report.submitted_at is not None or any(s.report_id == report.id for s in earlier):
            raise ReportAlreadySubmitted("this report version has been submitted already")
        any_before = bool(earlier) or any(
            r.submitted_at is not None for r in uow.reports.for_event(event.id)
        )
        now = clock.now()
        submission = ReportSubmission(
            id=uuid4(),
            event_id=event.id,
            report_id=report.id,
            submitted_by=principal.actor_id,
            submitted_at=now,
            submission_kind=(
                SubmissionKind.FOLLOW_UP if any_before else SubmissionKind.INITIAL
            ).value,
            report_type=request.report_type.value,
            expected_next_report_date=request.expected_next_report_date,
            authority_reference=request.authority_reference,
        )
        uow.report_submissions.add(submission)
        report.submitted_at = now
        report.submitted_by = principal.actor_id
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.REPORT_SUBMITTED,
            actor_id=principal.actor_id,
            payload={
                "event_id": str(event.id),
                "submission_id": str(submission.id),
                "report_id": str(report.id),
                "disposition_seq": report.disposition_seq,
                "report_sha256": report.report_sha256,
                "submission_kind": submission.submission_kind,
                "report_type": submission.report_type,
                "submitted_at": _iso(now),
                "expected_next_report_date": (
                    request.expected_next_report_date.isoformat()
                    if request.expected_next_report_date
                    else None
                ),
                "authority_reference": request.authority_reference,
            },
            event_id=event.id,
        )
    return submission
