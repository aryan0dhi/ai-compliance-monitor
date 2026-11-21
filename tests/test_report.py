"""GET /v1/events/{id}/report and POST /v1/events/{id}/report/submission (REQ-08a, REQ-12,
REQ-13; B-12; AC-13; D-25).

Service rules on the in-memory unit of work, then the HTTP mapping with dependency overrides.
"""

import hashlib
import json
from datetime import timedelta
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
from app.modules.review.disposition import (
    DispositionRequest,
    FieldConfirmation,
    record_disposition,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus
from app.modules.review.report import (
    ReportDenied,
    ReportUnavailable,
    get_report,
    mark_report_submitted,
)
from app.modules.review.report_schemas import ReportOut, report_body
from tests.conftest import T0
from tests.fakes import RecordingNotifier, system_principal, user_principal

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


def submit(uow, ledger, clock, event_id, principal, note=None):
    return mark_report_submitted(
        event_id, note, principal=principal, uow=uow, ledger=ledger, clock=clock
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
        assert uow.reports.rows == {(disposed, 1): stored}

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
        assert uow.reports.get(disposed, 1).document == first.document

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
        report(uow, ledger, clock, disposed, reviewer)
        dispose(uow, ledger, clock, disposed, reviewer, NOT_SERIOUS, rationale="no harm after all")
        with pytest.raises(ReportUnavailable, match="NOT_SERIOUS"):
            report(uow, ledger, clock, disposed, reviewer)
        with pytest.raises(ReportUnavailable, match="NOT_SERIOUS"):
            submit(uow, ledger, clock, disposed, reviewer)

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
        with pytest.raises(ReportUnavailable, match="INV-12"):
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
    def test_marks_the_current_report_submitted(self, uow, ledger, clock, disposed, reviewer):
        stored = report(uow, ledger, clock, disposed, reviewer)
        document = stored.document
        clock.advance(hours=2)
        submitted = submit(uow, ledger, clock, disposed, reviewer, note="sent via the portal")

        assert submitted is stored
        assert submitted.submitted_at == T0.replace(minute=1) + timedelta(hours=6)
        assert submitted.submitted_by == reviewer.actor_id
        assert submitted.submission_note == "sent via the portal"
        assert submitted.document == document  # the document itself never changes
        assert ledger.calls[-1] == {
            "session": None,
            "entry_type": LedgerEntryType.REPORT_SUBMITTED,
            "actor_id": reviewer.actor_id,
            "payload": {
                "event_id": str(disposed),
                "disposition_seq": 1,
                "report_sha256": stored.report_sha256,
                "submitted_at": "2026-09-28T02:01:00+00:00",
                "note": "sent via the portal",
            },
            "event_id": disposed,
        }
        body = json.loads(report_body(submitted))
        assert body["submitted_at"] == "2026-09-28T02:01:00Z"
        # The event itself doesn't change state (D-03: report states are internal).
        assert uow.events.get(disposed).state == EventState.DISPOSITIONED

    def test_later_reads_return_the_submitted_report(self, uow, ledger, clock, disposed, reviewer):
        report(uow, ledger, clock, disposed, reviewer)
        submitted = report_body(submit(uow, ledger, clock, disposed, reviewer))
        assert report_body(report(uow, ledger, clock, disposed, reviewer)) == submitted

    def test_without_a_note(self, uow, ledger, clock, disposed, reviewer):
        report(uow, ledger, clock, disposed, reviewer)
        assert submit(uow, ledger, clock, disposed, reviewer).submission_note is None

    def test_before_assembly_is_409(self, uow, ledger, clock, disposed, reviewer):
        with pytest.raises(ReportUnavailable, match="GET it first"):
            submit(uow, ledger, clock, disposed, reviewer)
        assert ledger.calls == []

    def test_twice_is_409(self, uow, ledger, clock, disposed, reviewer):
        report(uow, ledger, clock, disposed, reviewer)
        first = submit(uow, ledger, clock, disposed, reviewer, note="first")
        calls = len(ledger.calls)
        clock.advance(hours=1)
        with pytest.raises(ReportUnavailable, match="already marked submitted"):
            submit(uow, ledger, clock, disposed, reviewer, note="second")
        assert first.submission_note == "first" and len(ledger.calls) == calls

    def test_revision_after_submission_needs_a_new_report(
        self, uow, ledger, clock, disposed, reviewer
    ):
        """D-25: a submitted report belongs to its determination; a revision's report starts
        unsubmitted and must be assembled before it can be marked."""
        report(uow, ledger, clock, disposed, reviewer)
        submit(uow, ledger, clock, disposed, reviewer)
        dispose(uow, ledger, clock, disposed, reviewer, DEATH)
        with pytest.raises(ReportUnavailable, match="GET it first"):
            submit(uow, ledger, clock, disposed, reviewer)
        revised = report(uow, ledger, clock, disposed, reviewer)
        assert revised.disposition_seq == 2 and revised.submitted_at is None
        assert uow.reports.get(disposed, 1).submitted_at is not None


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
        stored = uow.reports.get(disposed, 1)
        assert body.startswith(stored.document[:-1])
        assert (
            json.loads(body)["report_sha256"]
            == hashlib.sha256(stored.document.encode()).hexdigest()
        )

    def test_premature_report_is_409_with_error_body(self, http, event_id):
        resp = http.get(event_id)
        assert resp.status_code == 409
        assert set(resp.json()) == {"detail"}

    def test_viewer_is_403_even_for_an_unknown_event(self, http):
        http.principal = user_principal(Role.VIEWER)
        assert http.get(uuid4()).status_code == 403
        assert http.submit(uuid4()).status_code == 403

    @pytest.mark.parametrize("event_id", ["not-an-id", str(uuid4())])
    def test_unknown_or_malformed_id_is_404(self, http, event_id):
        assert http.get(event_id).status_code == 404
        assert http.submit(event_id).status_code == 404

    def test_submission_with_and_without_body(self, http, uow, disposed):
        assert http.get(disposed).status_code == 200
        resp = http.submit(disposed)
        assert resp.status_code == 200, resp.text
        assert resp.json()["submitted_at"] == "2026-09-28T00:01:00Z"
        assert uow.reports.get(disposed, 1).submission_note is None
        # Marked once; a repeat is 409.
        assert http.submit(disposed, json={"note": "again"}).status_code == 409

    def test_submission_returns_what_get_returns_next(self, http, disposed):
        http.get(disposed)
        submitted = http.submit(disposed, json={"note": "sent"})
        assert submitted.status_code == 200, submitted.text
        assert http.get(disposed).content == submitted.content

    def test_submission_before_get_is_409(self, http, disposed):
        assert http.submit(disposed, json={"note": "sent"}).status_code == 409

    def test_note_longer_than_2000_is_400(self, http, uow, disposed):
        http.get(disposed)
        resp = http.submit(disposed, json={"note": "x" * 2001})
        assert resp.status_code == 400
        assert uow.reports.get(disposed, 1).submitted_at is None
        assert http.submit(disposed, json={"note": "x" * 2000}).status_code == 200

    def test_operations_are_published(self):
        paths = create_app().openapi()["paths"]
        get = paths["/v1/events/{event_id}/report"]["get"]
        post = paths["/v1/events/{event_id}/report/submission"]["post"]
        assert get["operationId"] == "getReport"
        assert post["operationId"] == "markReportSubmitted"
        assert {"200", "401", "403", "404", "409"} <= set(get["responses"])
        assert {"200", "400", "401", "403", "404", "409"} <= set(post["responses"])


def re_sha(value: str) -> bool:
    return len(value) == 64 and set(value) <= set("0123456789abcdef")
