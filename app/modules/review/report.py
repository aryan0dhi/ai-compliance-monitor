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

from datetime import UTC, datetime
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event, EventType
from app.modules.events.service import EventNotFound, EventRecord
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.review.models import Disposition, DispositionTier, Report
from app.modules.review.report_schemas import assemble_document, sha256_hex

# B-13 / REQ-13: reports are for reviewers; viewers and admins get 403 like for dispositions.
REPORT_ROLES = frozenset({Role.REVIEWER})


class ReportDenied(Exception):
    """The caller's role may not read or submit reports (403)."""


class ReportUnavailable(Exception):
    """The event has no report in its current state, or it can't be marked submitted (409)."""


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
        raise ReportUnavailable("the event has no disposition yet (REQ-08a)")
    if not DispositionTier(history[-1].tier).is_serious:
        raise ReportUnavailable("the event's current tier is NOT_SERIOUS, so it has no report")
    return history


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def get_report(
    event_id: UUID,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> Report:
    """The report of the event's current determination, assembling and storing it first if
    needed. Raises ReportDenied (403), EventNotFound (404) or ReportUnavailable (409); nothing is
    written in those cases."""
    authorize_report(principal)
    with uow:
        event = _locked_event(uow, event_id)
        history = _serious_history(uow, event)
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
            return report
        now = clock.now()
        record = EventRecord(
            event=event, harm_fields=uow.harm_fields.for_event(event.id), dispositions=history
        )
        document = assemble_document(record, assembled_at=now)
        report = Report(
            event_id=event.id,
            disposition_seq=current.seq,
            document=document,
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
    return report


def mark_report_submitted(
    event_id: UUID,
    note: str | None,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> Report:
    """Record that the reviewer submitted the current report to the authority (outside the
    system). The report must have been assembled (GET) and not yet marked submitted, else 409."""
    authorize_report(principal)
    with uow:
        event = _locked_event(uow, event_id)
        current = _serious_history(uow, event)[-1]
        report = uow.reports.get(event.id, current.seq)
        if report is None:
            raise ReportUnavailable(
                "the report of the current determination hasn't been assembled; GET it first"
            )
        if report.submitted_at is not None:
            raise ReportUnavailable(
                f"the report was already marked submitted at {_iso(report.submitted_at)}"
            )
        now = clock.now()
        report.submitted_at = now
        report.submitted_by = principal.actor_id
        report.submission_note = note
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.REPORT_SUBMITTED,
            actor_id=principal.actor_id,
            payload={
                "event_id": str(event.id),
                "disposition_seq": report.disposition_seq,
                "report_sha256": report.report_sha256,
                "submitted_at": _iso(now),
                "note": note,
            },
            event_id=event.id,
        )
    return report
