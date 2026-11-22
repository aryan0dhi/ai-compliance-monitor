"""The Article 73 report document, `pod-h-report-1` (contract delta Δ1 `report.schema.json`;
REQ-12, REQ-15, AC-13, AC-17; D-30).

`assemble_document` renders a report once, at assembly. The stored text is what every later
`GET /v1/events/{id}/report` and `GET /v1/reports/{id}` returns, byte for byte, and
`X-Report-Sha256` is the SHA-256 of exactly those bytes. Field order is the schema's.

Where the schema leaves a choice, D-30 records ours:

- `report_kind` is always SERIOUS_INCIDENT and `submission_marking` null: a NOT_SERIOUS event has
  no report (Phase 2 Model 5 Ex-2, REQ-08a), so the schema's NOT_SERIOUS_CLOSURE branch is unused;
- `timestamps.determined_at` is `A`, the first serious-tier disposition (the anchor of the
  deadline, D-23); `determination.assigned_at` is the current disposition; `last_revised_at` is
  the current disposition once there has been a revision, else null;
- `risk_indicator.detection_to_assignment_seconds` is `A - detected_at` in whole seconds;
- `ledger_anchor` is the ledger's newest entry when the report was assembled; the report's own
  REPORT_ASSEMBLED entry, which carries the report's hash, comes after it.
"""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SerializationInfo, model_serializer

from app.core.types import IdOut, JsonDecimal, UtcDatetime
from app.modules.events.service import EventRecord
from app.modules.review.article73 import TemplateInputs, article73_template
from app.modules.review.deadlines import DEADLINE_DAYS
from app.modules.review.evidence import EvidenceBundle, format_utc
from app.modules.review.ids import disposition_public_id, report_public_id
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus

SCHEMA_VERSION: Literal["pod-h-report-1"] = "pod-h-report-1"
# deadlines.py: deadline_utc = A + days(tier), days of exactly 24 h, in UTC.
CALENDAR = "UTC calendar days of exactly 24 hours (deadline = first serious determination + days)"

# The schema's harm-field names inside reports (Art. 3(49)); requests and events keep ours.
REPORT_HARM_FIELDS: Mapping[HarmFieldName, str] = {
    HarmFieldName.HEALTH_SAFETY: "health_safety_harm",
    HarmFieldName.CRITICAL_INFRASTRUCTURE: "critical_infrastructure_disruption",
    HarmFieldName.FUNDAMENTAL_RIGHTS: "fundamental_rights_infringement",
    HarmFieldName.PROPERTY_ENVIRONMENT: "property_environmental_damage",
}

TIER_LABELS: Mapping[DispositionTier, str] = {
    DispositionTier.DEATH: "Death of a person (Article 73(4))",
    DispositionTier.CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD: (
        "Widespread infringement or serious and irreversible disruption of critical "
        "infrastructure (Article 73(3))"
    ),
    DispositionTier.OTHER_SERIOUS: "Other serious incident (Article 73(2))",
    DispositionTier.NOT_SERIOUS: "Not a serious incident",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceWindowOut(_Strict):
    start: str
    end: str


class EvidenceSampleOut(_Strict):
    sampled_at: str
    value: float
    snapshot_ref: str | None


class EvidenceBundleOut(_Strict):
    window: EvidenceWindowOut
    samples: list[EvidenceSampleOut]


class ReportEventOut(_Strict):
    event_id: IdOut
    system_id: IdOut
    type: Literal["threshold_breach"]
    metric_name: str
    observed_value: JsonDecimal
    threshold_value: JsonDecimal
    detected_at: UtcDatetime
    evidence_bundle: EvidenceBundleOut
    evidence_sha256: str


class ReportHarmFieldOut(_Strict):
    status: Literal["CONFIRMED", "NOT_APPLICABLE"]
    evidence: str | None
    confirmed_by: IdOut
    confirmed_at: UtcDatetime


class ReportHarmFieldsOut(_Strict):
    health_safety_harm: ReportHarmFieldOut
    critical_infrastructure_disruption: ReportHarmFieldOut
    fundamental_rights_infringement: ReportHarmFieldOut
    property_environmental_damage: ReportHarmFieldOut


class RevisionEntryOut(_Strict):
    prior_tier: DispositionTier
    new_tier: DispositionTier
    reviewer_id: IdOut
    revised_at: UtcDatetime


class DeterminationOut(_Strict):
    tier: DispositionTier
    tier_label: str
    rationale: str | None
    disposition_id: str
    reviewer_id: IdOut
    assigned_at: UtcDatetime
    deadline_utc: UtcDatetime | None
    calendar: str
    revisions: list[RevisionEntryOut]


class TimestampsOut(_Strict):
    detected_at: UtcDatetime
    determined_at: UtcDatetime
    last_revised_at: UtcDatetime | None
    assembled_at: UtcDatetime


class RiskIndicatorOut(_Strict):
    detection_to_assignment_seconds: int


class RegulationOut(_Strict):
    instrument: Literal["Regulation (EU) 2024/1689"] = "Regulation (EU) 2024/1689"
    reporting_article: Literal["Article 73"] = "Article 73"
    serious_incident_definition: Literal["Article 3(49)"] = "Article 3(49)"
    reporting_window_days: Literal[2, 10, 15] | None


class TemplateFieldOut(_Strict):
    field: str
    name: str
    status: Literal[
        "section", "group", "mapped", "derivable", "requires_reviewer_input", "not_available"
    ]
    value: Any
    note: str | None = None

    @model_serializer(mode="wrap")
    def _omit_missing_note(self, handler: Any, info: SerializationInfo) -> dict[str, Any]:
        # The schema's `note` is an optional string: absent, never null.
        data: dict[str, Any] = handler(self)
        if data.get("note") is None:
            data.pop("note", None)
        return data


class Article73TemplateOut(_Strict):
    template: str
    template_version: str
    template_reference: str
    fields: list[TemplateFieldOut]


class LedgerAnchorOut(_Strict):
    seq: int
    hash: str


class ReportDocument(_Strict):
    """The response schema of both report reads (Δ1). Routes send the stored bytes."""

    report_id: str
    report_version: int
    report_kind: Literal["SERIOUS_INCIDENT", "NOT_SERIOUS_CLOSURE"]
    schema_version: Literal["pod-h-report-1"]
    event: ReportEventOut
    harm_fields: ReportHarmFieldsOut
    determination: DeterminationOut
    timestamps: TimestampsOut
    risk_indicator: RiskIndicatorOut
    regulation: RegulationOut
    submission_marking: Literal["NOT FOR SUBMISSION"] | None
    article73_template: Article73TemplateOut
    ledger_anchor: LedgerAnchorOut


def _harm_fields(record: EventRecord) -> ReportHarmFieldsOut:
    out: dict[str, ReportHarmFieldOut] = {}
    for f in record.harm_fields:
        if f.status == HarmFieldStatus.PENDING or f.confirmed_by is None or f.confirmed_at is None:
            raise ValueError("a disposition needs every harm field confirmed first (B-08)")
        out[REPORT_HARM_FIELDS[HarmFieldName(f.name)]] = ReportHarmFieldOut(
            status=f.status,  # type: ignore[arg-type]
            evidence=record.evidence.get(f.name),
            confirmed_by=f.confirmed_by,
            confirmed_at=f.confirmed_at,
        )
    return ReportHarmFieldsOut(**out)


def assemble_document(
    record: EventRecord,
    *,
    report_id: Any,
    system_name: str,
    evidence: EvidenceBundle,
    ledger_head: tuple[int, str],
    assembled_at: datetime,
) -> str:
    """Render the report of the record's latest disposition, which must be a serious tier."""
    history = record.dispositions
    current = history[-1]
    tier = DispositionTier(current.tier)
    if not tier.is_serious or current.deadline_utc is None:
        raise ValueError("only a serious-tier determination has a report")
    event = record.event
    if (
        event.metric_name is None
        or event.observed_value is None
        or event.threshold_value is None
        or event.metric_timestamp is None
    ):  # pragma: no cover - every breach has them
        raise ValueError("a threshold breach has its metric, values and sample time")
    # A (D-23): the first serious-tier disposition; the deadline counts from it.
    anchor = next(d.recorded_at for d in history if DispositionTier(d.tier).is_serious)
    harm_fields = _harm_fields(record)
    detected_at = event.detected_at
    document = ReportDocument(
        report_id=report_public_id(report_id),
        report_version=current.seq,
        report_kind="SERIOUS_INCIDENT",
        schema_version=SCHEMA_VERSION,
        event=ReportEventOut(
            event_id=event.id,
            system_id=event.system_id,
            type="threshold_breach",
            metric_name=event.metric_name,
            observed_value=event.observed_value,
            threshold_value=event.threshold_value,
            detected_at=detected_at,
            evidence_bundle=EvidenceBundleOut.model_validate(evidence.as_json()),
            evidence_sha256=evidence.sha256,
        ),
        harm_fields=harm_fields,
        determination=DeterminationOut(
            tier=tier,
            tier_label=TIER_LABELS[tier],
            rationale=record.rationales.get(current.seq),
            disposition_id=disposition_public_id(event.id, current.seq),
            reviewer_id=current.reviewer_id,
            assigned_at=current.recorded_at,
            deadline_utc=current.deadline_utc,
            calendar=CALENDAR,
            revisions=[
                RevisionEntryOut(
                    prior_tier=DispositionTier(d.previous_tier),
                    new_tier=DispositionTier(d.tier),
                    reviewer_id=d.reviewer_id,
                    revised_at=d.recorded_at,
                )
                for d in history[1:]
                if d.previous_tier is not None
            ],
        ),
        timestamps=TimestampsOut(
            detected_at=detected_at,
            determined_at=anchor,
            last_revised_at=current.recorded_at if len(history) > 1 else None,
            assembled_at=assembled_at,
        ),
        risk_indicator=RiskIndicatorOut(
            detection_to_assignment_seconds=max(0, int((anchor - detected_at).total_seconds()))
        ),
        regulation=RegulationOut(reporting_window_days=DEADLINE_DAYS[tier]),  # type: ignore[arg-type]
        submission_marking=None,
        article73_template=Article73TemplateOut.model_validate(
            article73_template(
                TemplateInputs(
                    tier=tier,
                    harm_statuses={
                        HarmFieldName(f.name): HarmFieldStatus(f.status) for f in record.harm_fields
                    },
                    detected_at=format_utc(detected_at),  # as timestamps.detected_at
                    determined_at=anchor,
                    system_name=system_name,
                )
            )
        ),
        ledger_anchor=LedgerAnchorOut(seq=ledger_head[0], hash=ledger_head[1]),
    )
    return document.model_dump_json()
