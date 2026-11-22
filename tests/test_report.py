"""GET /v1/events/{id}/report and POST /v1/events/{id}/report/submission (REQ-08a, REQ-12,
REQ-13; B-12; AC-13; D-25).

Service rules on the in-memory unit of work, then the HTTP mapping with dependency overrides.
"""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.core.errors import UnimplementedError
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.events.service import EventNotFound
from app.modules.ledger.interface import LedgerEntryType, get_ledger
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.interface import get_notifier
from app.modules.records.service import erase_expired, report_purpose
from app.modules.review.disposition import (
    DispositionRequest,
    FieldConfirmation,
    record_disposition,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus, ReportType
from app.modules.review.report import (
    NotSubmittable,
    RecordErased,
    ReportAlreadySubmitted,
    ReportDenied,
    ReportHashMismatch,
    ReportNotCurrent,
    ReportNotFound,
    ReportStatus,
    ReportUnavailable,
    SubmissionRequest,
    get_report,
    get_report_version,
    list_report_versions,
    submit_report,
)
from app.modules.review.report_schemas import ReportOut, report_body
from tests.conftest import T0
from tests.fakes import RecordingNotifier, opened, system_principal, user_principal

SYSTEM = UUID("550e8400-e29b-41d4-a716-446655440000")
CONFIRMED, NA = HarmFieldStatus.CONFIRMED, HarmFieldStatus.NOT_APPLICABLE
OTHER, DEATH, NOT_SERIOUS = (
    DispositionTier.OTHER_SERIOUS,
    DispositionTier.DEATH,
    DispositionTier.NOT_SERIOUS,
)
# Model 5 Ex-1: fundamental_rights confirmed, the other three not applicable.
EX1_FIELDS = {
    HarmFieldName.FUNDAMENTAL_RIGHTS: FieldConfirmation(CONFIRMED, "credit denied to applicants"),
    HarmFieldName.HEALTH_SAFETY: FieldConfirmation(NA),
    HarmFieldName.CRITICAL_INFRASTRUCTURE: FieldConfirmation(NA),
    HarmFieldName.PROPERTY_ENVIRONMENT: FieldConfirmation(NA),
}


@pytest.fixture
def reviewer():
    return user_principal(Role.REVIEWER)


@pytest.fixture
def event_id(uow, ledger, clock) -> UUID:
    """A FLAGGED loan-scorer accuracy breach with its four PENDING harm fields (Model 5 Ex-1)."""
    uow.seed_system(SYSTEM, harm_fields=["fundamental_rights"])
    uow.seed_threshold(
        SYSTEM,
        "accuracy",
        metric_type="performance",
        direction="below",
        limit="0.70",
        valid_from=T0 - timedelta(days=1),
    )
    result = ingest_metric(
        MetricSubmission(
            system_id=SYSTEM,
            metric_name="accuracy",
            metric_type=MetricType.PERFORMANCE,
            value=Decimal("0.61"),
            sampled_at=T0,
        ),
        principal=system_principal(SYSTEM),
        uow=uow,
        ledger=ledger,
        notifier=RecordingNotifier(),
        clock=clock,
    )
    assert result.flagged_event_id is not None
    ledger.calls.clear()
    return result.flagged_event_id


def dispose(uow, ledger, clock, event_id, principal, tier, *, rationale=None, fields=None):
    return record_disposition(
        event_id,
        DispositionRequest(tier=tier, rationale=rationale, harm_fields=fields or {}),
        principal=principal,
        uow=uow,
        ledger=ledger,
        clock=clock,
    )


@pytest.fixture
def disposed(uow, ledger, clock, event_id, reviewer) -> UUID:
    """Ex-1 at d0 13:00: fields confirmed, OTHER_SERIOUS assigned (deadline A + 15 days)."""
    clock.advance(hours=4)
    dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
    ledger.calls.clear()
    return event_id


def report(uow, ledger, clock, event_id, principal):
    return get_report(event_id, principal=principal, uow=uow, ledger=ledger, clock=clock)


def submit(uow, ledger, clock, event_id, principal, version=None, *, json_sha256=None, **extra):
    """Contract delta \u03943: record a submission of `version` (a report as GET returns it).
    Without one, a report_id that no event has."""
    request = SubmissionRequest(
        report_id=version.report_id if version is not None else uuid4(),
        json_sha256=json_sha256 or (version.report_sha256 if version is not None else "0" * 64),
        report_type=extra.pop("report_type", ReportType.INITIAL),
        **extra,
    )
    return submit_report(
        event_id, request, principal=principal, uow=uow, ledger=ledger, clock=clock
    )


class TestAssembly:
    def test_first_request_assembles_the_report(self, uow, ledger, clock, disposed, reviewer):
        """AC-13: the event, the four harm fields, the determination and the three times."""
        clock.advance(minutes=30)
        stored = report(uow, ledger, clock, disposed, reviewer)

        doc = json.loads(stored.document)
        assert list(doc) == [
            "event",
            "harm_fields",
            "determination",
            "detected_at",
            "determination_at",
            "assembled_at",
            "deadline_utc",
        ]
        assert doc["event"]["event_id"] == str(disposed)
        assert doc["event"]["state"] == "DISPOSITIONED"
        assert [r["tier"] for r in doc["event"]["revision_history"]] == ["OTHER_SERIOUS"]
        assert list(doc["harm_fields"]) == [n.value for n in HarmFieldName]
        assert doc["harm_fields"]["fundamental_rights"]["status"] == "CONFIRMED"
        assert doc["harm_fields"] == doc["event"]["harm_fields"]
        assert doc["determination"] == {
            "tier": "OTHER_SERIOUS",
            "reviewer_id": str(reviewer.actor_id),
            "rationale": None,
        }
        assert doc["detected_at"] == "2026-09-27T20:01:00Z"
        assert doc["determination_at"] == "2026-09-28T00:01:00Z"
        assert doc["assembled_at"] == "2026-09-28T00:31:00Z"
        assert doc["deadline_utc"] == "2026-10-13T00:01:00Z"  # B-10: A + 15 days
        assert stored.report_sha256 == hashlib.sha256(stored.document.encode()).hexdigest()
        assert (stored.disposition_seq, stored.assembled_by) == (1, reviewer.actor_id)
        assert stored.submitted_at is None
        # The row holds the same document sealed with the event's key (REQ-16).
        (row,) = uow.reports.rows.values()
        assert (row.event_id, row.disposition_seq, row.report_sha256) == (
            disposed,
            1,
            stored.report_sha256,
        )
        assert opened(uow, disposed, report_purpose(1), row.document) == stored.document

    def test_assembly_writes_one_ledger_entry(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        assert ledger.calls == [
            {
                "session": None,
                "entry_type": LedgerEntryType.REPORT_ASSEMBLED,
                "actor_id": reviewer.actor_id,
                "payload": {
                    "event_id": str(disposed),
                    "disposition_seq": 1,
                    "tier": "OTHER_SERIOUS",
                    "assembled_at": "2026-09-28T00:01:00+00:00",
                    "report_sha256": stored.report_sha256,
                },
                "event_id": disposed,
            }
        ]

    def test_repeated_requests_return_the_same_bytes(self, uow, ledger, clock, disposed, reviewer):
        """B-12: assembled once; later calls (by anyone, at any time) return the stored bytes
        and each add one VIEW entry (B-14)."""
        first = report_body(report(uow, ledger, clock, disposed, reviewer))
        clock.advance(days=2)
        other = user_principal(Role.REVIEWER)
        second = report_body(report(uow, ledger, clock, disposed, other))
        assert second == first
        assert len(uow.reports.rows) == 1
        assert [c["entry_type"] for c in ledger.calls] == [
            LedgerEntryType.REPORT_ASSEMBLED,
            LedgerEntryType.VIEW,
        ]
        view = ledger.calls[1]
        assert view["actor_id"] == other.actor_id
        sha = json.loads(second)["report_sha256"]
        assert view["payload"] == {"event_id": str(disposed), "report_sha256": sha}

    def test_body_is_the_document_plus_hash_and_submission(
        self, uow, ledger, clock, disposed, reviewer
    ):
        stored = report(uow, ledger, clock, disposed, reviewer)
        body = report_body(stored).decode()
        assert body.startswith(stored.document[:-1] + ",")
        parsed = json.loads(body)
        assert parsed["report_sha256"] == stored.report_sha256
        assert parsed["submitted_at"] is None
        ReportOut.model_validate_json(body)

    def test_revision_after_assembly_gets_a_new_report(
        self, uow, ledger, clock, disposed, reviewer
    ):
        """D-25: the report follows the current determination; the earlier one is kept."""
        first = report(uow, ledger, clock, disposed, reviewer)
        clock.advance(days=3)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        second = report(uow, ledger, clock, disposed, reviewer)

        assert second.disposition_seq == 2 and second.report_sha256 != first.report_sha256
        doc = json.loads(second.document)
        assert doc["determination"]["tier"] == "DEATH"
        assert doc["determination_at"] == "2026-10-01T00:01:00Z"
        assert doc["deadline_utc"] == "2026-10-08T00:01:00Z"  # stricter: A + 10 days
        assert [r["tier"] for r in doc["event"]["revision_history"]] == ["OTHER_SERIOUS", "DEATH"]
        assert set(uow.reports.rows) == {(disposed, 1), (disposed, 2)}
        kept = uow.reports.get(disposed, 1).document
        assert opened(uow, disposed, report_purpose(1), kept) == first.document

    def test_detected_at_never_changes(self, uow, ledger, clock, disposed, reviewer):
        """B-15: detected_at in the report is the flag time, not the determination's."""
        detected = uow.events.get(disposed).detected_at
        doc = json.loads(report(uow, ledger, clock, disposed, reviewer).document)
        assert doc["detected_at"] == doc["event"]["detected_at"]
        assert detected == T0.replace(minute=1)


class TestRefusals:
    def test_no_disposition_is_409(self, uow, ledger, clock, event_id, reviewer):
        """REQ-08a / Ex-4 premature-report: no report before a serious-tier determination."""
        with pytest.raises(ReportUnavailable, match="no disposition"):
            report(uow, ledger, clock, event_id, reviewer)
        assert uow.reports.rows == {} and ledger.calls == []

    def test_not_serious_is_409(self, uow, ledger, clock, event_id, reviewer):
        """Ex-2 no-report: a NOT_SERIOUS determination closes the event without a report."""
        all_na = {name: FieldConfirmation(NA) for name in HarmFieldName}
        dispose(uow, ledger, clock, event_id, reviewer, NOT_SERIOUS, rationale="no", fields=all_na)
        ledger.calls.clear()
        with pytest.raises(ReportUnavailable, match="NOT_SERIOUS"):
            report(uow, ledger, clock, event_id, reviewer)
        assert uow.reports.rows == {} and ledger.calls == []

    def test_revised_to_not_serious_is_409(self, uow, ledger, clock, disposed, reviewer):
        """A report assembled earlier isn't served once the current tier is NOT_SERIOUS."""
        first = report(uow, ledger, clock, disposed, reviewer)
        dispose(uow, ledger, clock, disposed, reviewer, NOT_SERIOUS, rationale="no harm after all")
        with pytest.raises(ReportUnavailable, match="NOT_SERIOUS"):
            report(uow, ledger, clock, disposed, reviewer)
        # \u03943: the earlier version can't be submitted either.
        with pytest.raises(NotSubmittable):
            submit(uow, ledger, clock, disposed, reviewer, first)

    def test_monitoring_gap_is_409(self, uow, ledger, clock, reviewer):
        """INV-12: a gap is never reviewed, so it never has a report."""
        uow.seed_system(SYSTEM)
        gap = Event(
            id=uuid4(),
            type=EventType.MONITORING_GAP.value,
            state=EventState.REVIEWER_NOTIFIED.value,
            system_id=SYSTEM,
            detected_at=T0,
            retain_until=retain_until_for(T0),
            gap_started_at=T0 - timedelta(minutes=15),
        )
        uow.events.add(gap)
        with pytest.raises(ReportUnavailable, match="INV-12"):
            report(uow, ledger, clock, gap.id, reviewer)
        # A gap has no report version, so any report_id is unknown for it.
        with pytest.raises(ReportNotFound):
            submit(uow, ledger, clock, gap.id, reviewer)

    def test_unknown_event_is_404(self, uow, ledger, clock, reviewer):
        with pytest.raises(EventNotFound):
            report(uow, ledger, clock, uuid4(), reviewer)
        with pytest.raises(EventNotFound):
            submit(uow, ledger, clock, uuid4(), reviewer)

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
    def test_only_reviewers(self, uow, ledger, clock, disposed, role):
        """REQ-13 / B-13: viewers and admins get 403 and nothing is written."""
        with pytest.raises(ReportDenied):
            report(uow, ledger, clock, disposed, user_principal(role))
        with pytest.raises(ReportDenied):
            submit(uow, ledger, clock, disposed, user_principal(role))
        assert uow.reports.rows == {} and ledger.calls == []

    def test_system_keys_are_refused(self, uow, ledger, clock, disposed):
        with pytest.raises(ReportDenied):
            report(uow, ledger, clock, disposed, system_principal(SYSTEM))

    def test_ledger_failure_stores_nothing(self, uow, ledger, clock, disposed, reviewer):
        ledger.fail_with = UnimplementedError("x", "REQ-06", "ledger")
        with pytest.raises(UnimplementedError):
            report(uow, ledger, clock, disposed, reviewer)
        assert uow.reports.rows == {}
        assert uow.rollbacks == 1


class TestSubmission:
    """Contract delta \u03943 (D-29): a submission names the report version and its hash."""

    def test_records_an_initial_submission(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        clock.advance(hours=2)
        ledger.calls.clear()
        sub = submit(
            uow,
            ledger,
            clock,
            disposed,
            reviewer,
            stored,
            expected_next_report_date=date(2026, 10, 5),
            authority_reference="MSA-2026-0042",
        )
        assert (sub.event_id, sub.report_id, sub.submitted_by) == (
            disposed,
            stored.report_id,
            reviewer.actor_id,
        )
        assert sub.submitted_at == T0.replace(minute=1) + timedelta(hours=6)
        assert (sub.submission_kind, sub.report_type) == ("INITIAL", "Initial")
        assert sub.expected_next_report_date == date(2026, 10, 5)
        assert sub.authority_reference == "MSA-2026-0042"
        assert ledger.calls == [
            {
                "session": None,
                "entry_type": LedgerEntryType.REPORT_SUBMITTED,
                "actor_id": reviewer.actor_id,
                "payload": {
                    "event_id": str(disposed),
                    "submission_id": str(sub.id),
                    "report_id": str(stored.report_id),
                    "disposition_seq": 1,
                    "report_sha256": stored.report_sha256,
                    "submission_kind": "INITIAL",
                    "report_type": "Initial",
                    "submitted_at": "2026-09-28T02:01:00+00:00",
                    "expected_next_report_date": "2026-10-05",
                    "authority_reference": "MSA-2026-0042",
                },
                "event_id": disposed,
            }
        ]

    def test_later_reads_show_the_submission(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        submit(uow, ledger, clock, disposed, reviewer, stored)
        again = report(uow, ledger, clock, disposed, reviewer)
        assert again.document == stored.document  # the document itself never changes
        assert json.loads(report_body(again))["submitted_at"] == "2026-09-28T00:01:00Z"

    def test_a_revision_is_submitted_as_a_follow_up(self, uow, ledger, clock, disposed, reviewer):
        first = report(uow, ledger, clock, disposed, reviewer)
        submit(uow, ledger, clock, disposed, reviewer, first)
        clock.advance(days=3)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        second = report(uow, ledger, clock, disposed, reviewer)
        follow = submit(
            uow, ledger, clock, disposed, reviewer, second, report_type=ReportType.FOLLOW_UP
        )
        assert (follow.submission_kind, follow.report_type) == ("FOLLOW_UP", "Follow up")

    def test_a_superseded_version_is_not_current(self, uow, ledger, clock, disposed, reviewer):
        first = report(uow, ledger, clock, disposed, reviewer)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        with pytest.raises(ReportNotCurrent):
            submit(uow, ledger, clock, disposed, reviewer, first)

    def test_a_wrong_hash_is_refused(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        with pytest.raises(ReportHashMismatch):
            submit(uow, ledger, clock, disposed, reviewer, stored, json_sha256="a" * 64)
        assert uow.report_submissions.rows == []

    def test_twice_is_already_submitted(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        submit(uow, ledger, clock, disposed, reviewer, stored)
        calls = len(ledger.calls)
        with pytest.raises(ReportAlreadySubmitted):
            submit(uow, ledger, clock, disposed, reviewer, stored)
        assert len(ledger.calls) == calls and len(uow.report_submissions.rows) == 1

    def test_a_report_of_another_event_is_unknown(
        self, uow, ledger, clock, disposed, event_id, reviewer
    ):
        stored = report(uow, ledger, clock, disposed, reviewer)
        other = uuid4()
        uow.events.add(
            Event(
                id=other,
                type=EventType.THRESHOLD_BREACH.value,
                state=EventState.FLAGGED.value,
                system_id=SYSTEM,
                detected_at=T0,
                retain_until=retain_until_for(T0),
            )
        )
        with pytest.raises(ReportNotFound):
            submit(uow, ledger, clock, other, reviewer, stored)

    def test_after_erasure_it_is_gone(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        with pytest.raises(RecordErased):
            submit(uow, ledger, clock, disposed, reviewer, stored)


class TestVersions:
    """Contract delta \u03944: report versions by event and by id."""

    def test_statuses_follow_revisions_and_submissions(
        self, uow, ledger, clock, disposed, reviewer
    ):
        first = report(uow, ledger, clock, disposed, reviewer)
        viewer = user_principal(Role.VIEWER)
        (only,) = list_report_versions(disposed, principal=viewer, uow=uow)
        assert only.status is ReportStatus.ASSEMBLED
        submit(uow, ledger, clock, disposed, reviewer, first)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        report(uow, ledger, clock, disposed, reviewer)
        statuses = [v.status for v in list_report_versions(disposed, principal=viewer, uow=uow)]
        assert statuses == [ReportStatus.SUBMITTED_SUPERSEDED, ReportStatus.ASSEMBLED]
        dispose(uow, ledger, clock, disposed, reviewer, NOT_SERIOUS, rationale="no harm")
        statuses = [v.status for v in list_report_versions(disposed, principal=viewer, uow=uow)]
        assert statuses == [ReportStatus.SUBMITTED_SUPERSEDED, ReportStatus.SUPERSEDED]

    def test_listing_roles(self, uow, ledger, clock, disposed, reviewer):
        report(uow, ledger, clock, disposed, reviewer)
        assert len(list_report_versions(disposed, principal=reviewer, uow=uow)) == 1
        for principal in (user_principal(Role.ADMIN), system_principal(SYSTEM)):
            with pytest.raises(ReportDenied):
                list_report_versions(disposed, principal=principal, uow=uow)
        with pytest.raises(EventNotFound):
            list_report_versions(uuid4(), principal=reviewer, uow=uow)

    def test_any_version_by_id_with_a_view_entry(self, uow, ledger, clock, disposed, reviewer):
        first = report(uow, ledger, clock, disposed, reviewer)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        ledger.calls.clear()
        again = get_report_version(first.report_id, principal=reviewer, uow=uow, ledger=ledger)
        assert report_body(again) == report_body(first)
        (view,) = ledger.calls
        assert view["entry_type"] == LedgerEntryType.VIEW
        assert view["payload"]["report_id"] == str(first.report_id)

    def test_by_id_is_reviewer_only_404_and_410(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        with pytest.raises(ReportDenied):
            get_report_version(
                stored.report_id, principal=user_principal(Role.VIEWER), uow=uow, ledger=ledger
            )
        with pytest.raises(ReportNotFound):
            get_report_version(uuid4(), principal=reviewer, uow=uow, ledger=ledger)
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        with pytest.raises(RecordErased):
            get_report_version(stored.report_id, principal=reviewer, uow=uow, ledger=ledger)


# --- HTTP ------------------------------------------------------------------------------------


class Http:
    def __init__(self, uow, ledger, clock, principal) -> None:
        self.principal = principal
        app = create_app()
        app.dependency_overrides.update(
            {
                get_principal: lambda: self.principal,
                get_uow: lambda: uow,
                get_ledger: lambda: ledger,
                get_notifier: lambda: RecordingNotifier(),
                get_clock: lambda: clock,
            }
        )
        self.client = TestClient(app)

    def get(self, event_id):
        return self.client.get(f"/v1/events/{event_id}/report")

    def submit(self, event_id, **kwargs):
        return self.client.post(f"/v1/events/{event_id}/report/submission", **kwargs)


@pytest.fixture
def http(uow, ledger, clock, reviewer) -> Http:
    return Http(uow, ledger, clock, reviewer)


class TestHttp:
    def test_get_returns_the_report(self, http, disposed, reviewer):
        resp = http.get(disposed)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "application/json"
        body = resp.json()
        for name in ("detected_at", "determination_at", "assembled_at", "deadline_utc"):
            assert body[name] is not None, name
        assert body["determination"]["reviewer_id"] == str(reviewer.actor_id)
        assert body["event"]["event_id"] == str(disposed)
        assert body["submitted_at"] is None
        assert re_sha(body["report_sha256"])

    def test_get_is_byte_for_byte_stable(self, http, disposed, clock):
        first = http.get(disposed)
        clock.advance(hours=1)
        second = http.get(disposed)
        assert first.status_code == second.status_code == 200
        assert first.content == second.content

    def test_hash_is_over_the_stored_document(self, http, uow, disposed):
        body = http.get(disposed).content.decode()
        document = opened(uow, disposed, report_purpose(1), uow.reports.get(disposed, 1).document)
        assert body.startswith(document[:-1])
        assert json.loads(body)["report_sha256"] == hashlib.sha256(document.encode()).hexdigest()

    def test_premature_report_is_409_with_error_body(self, http, ledger, event_id, reviewer):
        resp = http.get(event_id)
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "NO_DISPOSITION"  # contract delta \u03942
        # D-26 / Ex-4 premature-report: the attempt is in the ledger, attributed to the caller.
        (call,) = ledger.calls
        assert call["entry_type"] == LedgerEntryType.REFUSED
        assert (call["event_id"], call["actor_id"]) == (event_id, reviewer.actor_id)
        assert call["payload"]["operation"] == "getReport"
        assert call["payload"]["status"] == 409

    def test_refused_submission_is_recorded(self, http, ledger, disposed):
        http.principal = user_principal(Role.VIEWER)
        assert http.submit(disposed, json=SUBMISSION).status_code == 403
        assert [(c["entry_type"], c["payload"]["operation"]) for c in ledger.calls] == [
            (LedgerEntryType.REFUSED, "submitReport")
        ]

    def test_viewer_is_403_even_for_an_unknown_event(self, http):
        http.principal = user_principal(Role.VIEWER)
        assert http.get(uuid4()).status_code == 403
        assert http.submit(uuid4(), json=SUBMISSION).status_code == 403

    @pytest.mark.parametrize("event_id", ["not-an-id", str(uuid4())])
    def test_unknown_or_malformed_id_is_404(self, http, event_id):
        assert http.get(event_id).status_code == 404
        assert http.submit(event_id, json=SUBMISSION).status_code == 404

    def test_submission_over_http(self, http, uow, disposed):
        version = http.get(disposed).json()
        body = {**SUBMISSION, "report_id": str(uow.reports.get(disposed, 1).id)}
        body["json_sha256"] = version["report_sha256"]
        resp = http.submit(disposed, json={**body, "authority_reference": "MSA-1"})
        assert resp.status_code == 201, resp.text
        out = resp.json()
        assert out["submission_kind"] == "INITIAL" and out["report_type"] == "Initial"
        assert out["event_id"] == str(disposed) and out["report_id"] == body["report_id"]
        assert set(out) == {
            "submission_id",
            "report_id",
            "event_id",
            "submitted_by",
            "submitted_at",
            "submission_kind",
            "report_type",
            "expected_next_report_date",
            "authority_reference",
        }
        again = http.submit(disposed, json=body)
        assert again.status_code == 409
        assert again.json()["error_code"] == "REPORT_ALREADY_SUBMITTED"
        assert http.get(disposed).json()["submitted_at"] == out["submitted_at"]

    def test_hash_mismatch_over_http(self, http, uow, disposed):
        http.get(disposed)
        body = {**SUBMISSION, "report_id": str(uow.reports.get(disposed, 1).id)}
        resp = http.submit(disposed, json=body)
        assert resp.status_code == 409 and resp.json()["error_code"] == "REPORT_HASH_MISMATCH"

    @pytest.mark.parametrize(
        "change",
        [
            {"note": "the Phase 4 body"},
            {"report_type": "Interim"},
            {"json_sha256": "not-hex"},
            {"expected_next_report_date": "next week"},
            {"authority_reference": "x" * 201},
        ],
    )
    def test_malformed_bodies_are_400(self, http, uow, disposed, change):
        http.get(disposed)
        body = {**SUBMISSION, "report_id": str(uow.reports.get(disposed, 1).id), **change}
        if "note" in change:
            body = change
        resp = http.submit(disposed, json=body)
        assert resp.status_code == 400 and resp.json()["error_code"] == "MALFORMED_BODY"
        assert uow.report_submissions.rows == []

    def test_versions_and_by_id_over_http(self, http, uow, disposed):
        current = http.get(disposed)
        versions = http.client.get(f"/v1/events/{disposed}/reports").json()
        assert [(v["report_version"], v["status"]) for v in versions] == [(1, "ASSEMBLED")]
        by_id = http.client.get(f"/v1/reports/{versions[0]['report_id']}")
        assert by_id.status_code == 200 and by_id.content == current.content
        assert http.client.get(f"/v1/reports/{uuid4()}").status_code == 404
        http.principal = user_principal(Role.VIEWER)
        assert http.client.get(f"/v1/events/{disposed}/reports").status_code == 200
        assert http.client.get(f"/v1/reports/{versions[0]['report_id']}").status_code == 403

    def test_by_id_after_erasure_is_410(self, http, uow, clock, ledger, disposed):
        http.get(disposed)
        report_id = uow.reports.get(disposed, 1).id
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        resp = http.client.get(f"/v1/reports/{report_id}")
        assert resp.status_code == 410 and resp.json()["error_code"] == "RECORD_ERASED"

    def test_report_headers(self, http, uow, disposed):
        """Contract delta \u03942: ETag, X-Report-Id, X-Report-Version, X-Report-Sha256."""
        resp = http.get(disposed)
        row = uow.reports.get(disposed, 1)
        assert resp.headers["etag"] == f'"{row.report_sha256}"'
        assert resp.headers["x-report-id"] == str(row.id)
        assert resp.headers["x-report-version"] == "1"
        assert resp.headers["x-report-sha256"] == resp.json()["report_sha256"]
        by_id = http.client.get(f"/v1/reports/{row.id}")
        assert by_id.headers["x-report-id"] == str(row.id)

    def test_format_parameter(self, http, disposed):
        default = http.get(disposed)
        as_json = http.client.get(f"/v1/events/{disposed}/report", params={"format": "json"})
        assert as_json.status_code == 200 and as_json.content == default.content
        pdf = http.client.get(f"/v1/events/{disposed}/report", params={"format": "pdf"})
        assert pdf.status_code == 501 and "PDF" in pdf.json()["detail"]
        bad = http.client.get(f"/v1/events/{disposed}/report", params={"format": "xml"})
        assert bad.status_code == 400 and bad.json()["error_code"] == "MALFORMED_QUERY"

    def test_operations_are_published(self):
        paths = create_app().openapi()["paths"]
        get = paths["/v1/events/{event_id}/report"]["get"]
        post = paths["/v1/events/{event_id}/report/submission"]["post"]
        assert get["operationId"] == "getReport"
        assert post["operationId"] == "submitReport"
        assert {"200", "401", "403", "404", "409"} <= set(get["responses"])
        assert {"201", "400", "401", "403", "404", "409", "410"} <= set(post["responses"])
        assert "get" in paths["/v1/events/{event_id}/reports"]
        assert {"200", "403", "404", "410"} <= set(
            paths["/v1/reports/{report_id}"]["get"]["responses"]
        )

    @pytest.mark.parametrize(
        "path", ["/v1/events/{e}/reports", "/v1/reports/{e}", "/v1/events/{e}/report/submission"]
    )
    def test_new_routes_are_401_without_a_key(self, path):
        url = path.format(e=uuid4())
        client = TestClient(create_app())
        resp = client.post(url, json=SUBMISSION) if "submission" in url else client.get(url)
        assert resp.status_code == 401


SUBMISSION = {"report_id": str(UUID(int=1)), "json_sha256": "0" * 64, "report_type": "Initial"}


def re_sha(value: str) -> bool:
    return len(value) == 64 and set(value) <= set("0123456789abcdef")
