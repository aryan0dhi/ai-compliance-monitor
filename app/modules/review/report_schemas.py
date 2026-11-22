"""The Article 73 report document (pod interface Report; REQ-12, B-12, D-25).

`assemble_document` renders the report once, at assembly; the stored text is what every later
`GET /v1/events/{id}/report` returns. `report_body` adds the two fields that aren't part of the
stored document (its hash, and when it was marked submitted) without re-rendering it, so the
bytes before them never change.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.types import IdOut, OpaqueId, UtcDatetime
from app.modules.events.schemas import EventDetail, HarmFieldOut
from app.modules.events.service import EventRecord
from app.modules.review.ids import report_public_id
from app.modules.review.models import DispositionTier, ReportSubmission, ReportType, SubmissionKind

AUTHORITY_REFERENCE_MAX_LENGTH = 200


class DeterminationOut(BaseModel):
    """The serious-tier disposition the report is about (REQ-08a)."""

    tier: DispositionTier
    reviewer_id: IdOut
    rationale: str | None


class ReportDocument(BaseModel):
    """What is stored at assembly. Field order is the order of the JSON text."""

    # The event as it stood at assembly: state, harm fields and revision history.
    event: EventDetail
    harm_fields: dict[str, HarmFieldOut]
    determination: DeterminationOut
    detected_at: UtcDatetime
    # recorded_at of the disposition reported on.
    determination_at: UtcDatetime
    assembled_at: UtcDatetime
    deadline_utc: UtcDatetime


class ReportTail(BaseModel):
    report_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    submitted_at: UtcDatetime | None = None


class ReportOut(ReportDocument, ReportTail):
    """The response schema (pod Report). Routes send `report_body` bytes, not this model."""


@dataclass(frozen=True)
class RevealedReport:
    """A stored report with its sealed text opened (REQ-16): what the routes serve. The report
    row itself holds the document and note sealed with the event's key."""

    report_id: UUID
    event_id: UUID
    disposition_seq: int
    document: str
    report_sha256: str
    assembled_at: datetime
    assembled_by: UUID
    submitted_at: datetime | None
    submitted_by: UUID | None
    submission_note: str | None


class ReportSubmissionIn(BaseModel):
    """Contract delta Δ3. `json_sha256` must be the version's `report_sha256`. `report_id` is
    the `rpt-` id, or the UUID form served before D-30."""

    report_id: OpaqueId
    json_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    report_type: ReportType
    expected_next_report_date: date | None = None
    authority_reference: str | None = Field(
        None, min_length=1, max_length=AUTHORITY_REFERENCE_MAX_LENGTH
    )


class SubmissionOut(BaseModel):
    submission_id: IdOut
    report_id: IdOut
    event_id: IdOut
    submitted_by: IdOut
    submitted_at: UtcDatetime
    submission_kind: SubmissionKind
    report_type: ReportType
    expected_next_report_date: date | None
    authority_reference: str | None

    @classmethod
    def from_submission(cls, s: ReportSubmission) -> "SubmissionOut":
        return cls(
            submission_id=s.id,
            report_id=report_public_id(s.report_id),
            event_id=s.event_id,
            submitted_by=s.submitted_by,
            submitted_at=s.submitted_at,
            submission_kind=SubmissionKind(s.submission_kind),
            report_type=ReportType(s.report_type),
            expected_next_report_date=s.expected_next_report_date,
            authority_reference=s.authority_reference,
        )


class ReportVersionOut(BaseModel):
    """One stored report version in GET /v1/events/{id}/reports (contract delta Δ4). The
    delta names only `status`; the other fields are ours (D-29)."""

    report_id: IdOut
    event_id: IdOut
    # The disposition it was assembled from (its version number).
    report_version: int
    status: str
    assembled_at: UtcDatetime
    report_sha256: str
    submitted_at: UtcDatetime | None


def assemble_document(record: EventRecord, *, assembled_at: datetime) -> str:
    """Render the report of the record's latest disposition, which must be a serious tier."""
    current = record.dispositions[-1]
    if not DispositionTier(current.tier).is_serious or current.deadline_utc is None:
        raise ValueError("only a serious-tier determination has a report")
    event = EventDetail.from_record(record)
    if event.harm_fields is None:  # pragma: no cover - a disposition opens all four fields
        raise ValueError("a disposed breach has harm fields")
    return ReportDocument(
        event=event,
        harm_fields=event.harm_fields,
        determination=DeterminationOut(
            tier=DispositionTier(current.tier),
            reviewer_id=current.reviewer_id,
            rationale=record.rationales.get(current.seq),
        ),
        detected_at=record.event.detected_at,
        determination_at=current.recorded_at,
        assembled_at=assembled_at,
        deadline_utc=current.deadline_utc,
    ).model_dump_json()


def sha256_hex(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def report_body(report: RevealedReport) -> bytes:
    """The stored document with report_sha256 and submitted_at appended, as response bytes."""
    tail = ReportTail(report_sha256=report.report_sha256, submitted_at=report.submitted_at)
    document = report.document
    if not (document.startswith("{") and document.endswith("}")):  # pragma: no cover
        raise ValueError("a stored report is a JSON object")
    return (document[:-1] + "," + tail.model_dump_json()[1:]).encode("utf-8")
