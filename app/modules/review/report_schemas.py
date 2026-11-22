"""Report response and submission schemas (REQ-12; contract delta Δ1–Δ4; D-25, D-30).

The report document itself is `report_document.ReportDocument`. The routes serve the stored
document bytes exactly as assembled (`report_body`), so `X-Report-Sha256` is the hash of the
response body; submission status is in the version list, not in the document.
"""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, Field

from app.core.types import IdOut, OpaqueId, UtcDatetime
from app.modules.review.ids import report_public_id
from app.modules.review.models import ReportSubmission, ReportType, SubmissionKind
from app.modules.review.report_document import ReportDocument

AUTHORITY_REFERENCE_MAX_LENGTH = 200


# The response schema of the report reads (Δ1 `pod-h-report-1`); routes send the stored bytes.
ReportOut = ReportDocument


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


def sha256_hex(document: str) -> str:
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def report_body(report: RevealedReport) -> bytes:
    """The stored document, byte for byte (Δ2: "the bytes are those stored at assembly")."""
    return report.document.encode("utf-8")
