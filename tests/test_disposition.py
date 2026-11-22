"""POST /v1/events/{id}/disposition (REQ-07, REQ-08a/b, REQ-09, REQ-13; B-08, B-09; AC-08 to AC-10).

Service rules on the in-memory unit of work, then the HTTP mapping with dependency overrides.
"""

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
from app.modules.events.schemas import EventDetail
from app.modules.events.service import EventNotFound, get_event
from app.modules.ledger.interface import LedgerEntryType, get_ledger
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.interface import get_notifier
from app.modules.records.service import evidence_purpose
from app.modules.review.disposition import (
    DispositionConflict,
    DispositionDenied,
    DispositionRequest,
    FieldConfirmation,
    MissingRationale,
    record_disposition,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus
from tests.conftest import T0
from tests.fakes import (
    RecordingLedger,
    RecordingNotifier,
    opened,
    system_principal,
    user_principal,
)

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


def assert_refusal_recorded(ledger, event_id, principal, status_code) -> None:
    """D-26: one REFUSED entry for the attempt (written after the rollback); then clears it."""
    (call,) = ledger.calls
    assert call["entry_type"] == LedgerEntryType.REFUSED
    assert (call["event_id"], call["actor_id"]) == (event_id, principal.actor_id)
    assert call["payload"]["operation"] == "recordDisposition"
    assert call["payload"]["status"] == status_code
    assert call["payload"]["role"] == principal.role.value
    ledger.calls.clear()


def assert_unchanged(uow, ledger, event_id) -> None:
    """Nothing was written: still FLAGGED, fields PENDING, no disposition, no ledger entry."""
    assert uow.events.get(event_id).state == EventState.FLAGGED
    fields = uow.harm_fields.for_event(event_id)
    assert {(f.status, f.confirmed_by, f.confirmed_at, f.evidence) for f in fields} == {
        (HarmFieldStatus.PENDING, None, None, None)
    }
    assert uow.dispositions.for_event(event_id) == []
    assert ledger.calls == []


class TestFirstDisposition:
    def test_serious_tier_confirms_fields_and_dispositions_the_event(
        self, uow, ledger, clock, event_id, reviewer
    ):
        """AC-08: all four fields confirmed or N/A, then a serious tier → DISPOSITIONED."""
        record = dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)

        assert record.event.state == EventState.DISPOSITIONED
        by_name = {f.name: f for f in record.harm_fields}
        assert by_name["fundamental_rights"].status == CONFIRMED
        # Stored sealed with the event's key (REQ-16).
        rights = by_name["fundamental_rights"].evidence
        assert opened(uow, event_id, evidence_purpose("fundamental_rights"), rights) == (
            "credit denied to applicants"
        )
        assert by_name["health_safety"].status == NA
        assert {(f.confirmed_by, f.confirmed_at) for f in record.harm_fields} == {
            (reviewer.actor_id, clock.now())
        }
        # D-20 prefill is kept next to the reviewer's evidence.
        assert by_name["fundamental_rights"].prefill_evidence is not None

        (d,) = uow.dispositions.for_event(event_id)
        assert (d.seq, d.tier, d.previous_tier, d.reviewer_id, d.recorded_at) == (
            1,
            "OTHER_SERIOUS",
            None,
            reviewer.actor_id,
            clock.now(),
        )

    def test_one_ledger_entry_records_the_whole_change(
        self, uow, ledger, clock, event_id, reviewer
    ):
        """B-14: one change, one entry, by the reviewer, with the confirmations it made."""
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        (entry,) = ledger.calls
        assert entry["entry_type"] == LedgerEntryType.DISPOSITION
        assert entry["actor_id"] == reviewer.actor_id and entry["event_id"] == event_id
        # The ledger holds the evidence sealed (REQ-16), the same token as the harm_field row.
        sealed = entry["payload"]["harm_fields"]["fundamental_rights"]["evidence"]
        assert opened(uow, event_id, evidence_purpose("fundamental_rights"), sealed) == (
            "credit denied to applicants"
        )
        assert entry["payload"] == {
            "event_id": str(event_id),
            "seq": 1,
            "tier": "OTHER_SERIOUS",
            "previous_tier": None,
            "rationale": None,
            "state": "DISPOSITIONED",
            "recorded_at": clock.now().isoformat(),
            "deadline_utc": (clock.now() + timedelta(days=15)).isoformat(),
            "harm_fields": {
                "health_safety": {"status": "NOT_APPLICABLE", "evidence": None},
                "critical_infrastructure": {"status": "NOT_APPLICABLE", "evidence": None},
                "fundamental_rights": {"status": "CONFIRMED", "evidence": sealed},
                "property_environment": {"status": "NOT_APPLICABLE", "evidence": None},
            },
        }

    def test_not_serious_with_rationale_closes_the_event(
        self, uow, ledger, clock, event_id, reviewer
    ):
        """AC-09 / Model 5 Ex-2: NOT_SERIOUS with a rationale → CLOSED, no deadline."""
        record = dispose(
            uow,
            ledger,
            clock,
            event_id,
            reviewer,
            NOT_SERIOUS,
            rationale="seasonal shift, no user impact",
            fields={name: FieldConfirmation(NA) for name in HarmFieldName},
        )
        assert record.event.state == EventState.CLOSED
        detail = EventDetail.from_record(record)
        assert detail.deadline_utc is None
        assert [r.rationale for r in detail.revision_history] == ["seasonal shift, no user impact"]

    @pytest.mark.parametrize("rationale", [None, "", "   \n\t"])
    def test_not_serious_needs_a_rationale(self, uow, ledger, clock, event_id, reviewer, rationale):
        """AC-09: a blank rationale is refused and changes nothing."""
        with pytest.raises(MissingRationale):
            dispose(
                uow,
                ledger,
                clock,
                event_id,
                reviewer,
                NOT_SERIOUS,
                rationale=rationale,
                fields={name: FieldConfirmation(NA) for name in HarmFieldName},
            )
        assert_unchanged(uow, ledger, event_id)

    def test_from_under_review(self, uow, ledger, clock, event_id, reviewer):
        uow.events.get(event_id).state = EventState.UNDER_REVIEW.value
        record = dispose(uow, ledger, clock, event_id, reviewer, DEATH, fields=EX1_FIELDS)
        assert record.event.state == EventState.DISPOSITIONED


class TestRefusals:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
    def test_only_reviewers(self, uow, ledger, clock, event_id, role):
        """REQ-13 / Model 5 Ex-4: a viewer (or admin) can't assign a tier."""
        with pytest.raises(DispositionDenied):
            dispose(uow, ledger, clock, event_id, user_principal(role), DEATH, fields=EX1_FIELDS)
        assert_unchanged(uow, ledger, event_id)

    def test_system_keys_are_refused(self, uow, ledger, clock, event_id):
        with pytest.raises(DispositionDenied):
            dispose(uow, ledger, clock, event_id, system_principal(SYSTEM), DEATH)
        assert_unchanged(uow, ledger, event_id)

    def test_role_is_checked_before_the_event(self, uow, ledger, clock):
        with pytest.raises(DispositionDenied):
            dispose(uow, ledger, clock, uuid4(), user_principal(Role.VIEWER), DEATH)

    def test_unknown_event(self, uow, ledger, clock, reviewer):
        with pytest.raises(EventNotFound):
            dispose(uow, ledger, clock, uuid4(), reviewer, DEATH, fields=EX1_FIELDS)

    def test_no_confirmations_is_409(self, uow, ledger, clock, event_id, reviewer):
        """AC-08: every field PENDING."""
        with pytest.raises(DispositionConflict, match="health_safety"):
            dispose(uow, ledger, clock, event_id, reviewer, OTHER)
        assert_unchanged(uow, ledger, event_id)

    def test_partial_confirmations_are_409_and_not_kept(
        self, uow, ledger, clock, event_id, reviewer
    ):
        """Three of four confirmed: refused, and the three are not kept either (one transaction)."""
        three = {k: v for k, v in EX1_FIELDS.items() if k != HarmFieldName.PROPERTY_ENVIRONMENT}
        with pytest.raises(DispositionConflict, match="pending: property_environment$"):
            dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=three)
        assert_unchanged(uow, ledger, event_id)

    def test_monitoring_gap_is_409(self, uow, ledger, clock, reviewer):
        """INV-12: a gap never takes a disposition."""
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
        with pytest.raises(DispositionConflict, match="INV-12"):
            dispose(uow, ledger, clock, gap.id, reviewer, DEATH, fields=EX1_FIELDS)
        assert gap.state == EventState.REVIEWER_NOTIFIED
        assert uow.harm_fields.rows == {} and uow.dispositions.rows == {}
        assert ledger.calls == []

    def test_ledger_failure_rolls_everything_back(self, uow, clock, event_id, reviewer):
        """Q7.2 Decision 1: no disposition without its audit entry."""
        failing = RecordingLedger(fail_with=UnimplementedError("x", "REQ-06", "ledger"))
        with pytest.raises(UnimplementedError):
            dispose(uow, failing, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        assert_unchanged(uow, failing, event_id)


class TestRevisions:
    def test_revision_appends_and_keeps_the_first(self, uow, ledger, clock, event_id, reviewer):
        """AC-10 / B-09 / Model 5 Ex-1: OTHER_SERIOUS, then DEATH three days later by another
        reviewer. Both are kept; the second names the tier it replaced."""
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        first_at = clock.now()
        clock.advance(days=3)
        second = user_principal(Role.REVIEWER)
        # Fields confirmed by the first call still count; this one confirms nothing.
        record = dispose(uow, ledger, clock, event_id, second, DEATH)

        first, revised = record.dispositions
        assert (first.seq, first.tier, first.previous_tier) == (1, "OTHER_SERIOUS", None)
        assert (first.reviewer_id, first.recorded_at) == (reviewer.actor_id, first_at)
        assert (revised.seq, revised.tier, revised.previous_tier) == (2, "DEATH", "OTHER_SERIOUS")
        assert (revised.reviewer_id, revised.recorded_at) == (second.actor_id, clock.now())
        assert record.event.state == EventState.DISPOSITIONED
        # The first call's confirmations are untouched.
        assert {f.confirmed_by for f in record.harm_fields} == {reviewer.actor_id}
        assert [c["payload"]["previous_tier"] for c in ledger.calls] == [None, "OTHER_SERIOUS"]
        assert [c["payload"]["harm_fields"] for c in ledger.calls][1] == {}

    def test_revision_to_not_serious_closes_and_back_reopens(
        self, uow, ledger, clock, event_id, reviewer
    ):
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        closed = dispose(uow, ledger, clock, event_id, reviewer, NOT_SERIOUS, rationale="no harm")
        assert closed.event.state == EventState.CLOSED
        reopened = dispose(uow, ledger, clock, event_id, reviewer, DEATH)
        assert reopened.event.state == EventState.DISPOSITIONED
        assert [(d.tier, d.previous_tier) for d in reopened.dispositions] == [
            ("OTHER_SERIOUS", None),
            ("NOT_SERIOUS", "OTHER_SERIOUS"),
            ("DEATH", "NOT_SERIOUS"),
        ]

    def test_same_tier_again_is_still_a_revision(self, uow, ledger, clock, event_id, reviewer):
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        record = dispose(uow, ledger, clock, event_id, reviewer, OTHER, rationale="re-checked")
        assert [(d.seq, d.previous_tier) for d in record.dispositions] == [
            (1, None),
            (2, "OTHER_SERIOUS"),
        ]

    def test_a_field_can_be_confirmed_again(self, uow, ledger, clock, event_id, reviewer):
        """A later call may change a confirmation; the ledger keeps both."""
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        clock.advance(hours=1)
        change = {HarmFieldName.HEALTH_SAFETY: FieldConfirmation(CONFIRMED, "patient harmed")}
        record = dispose(uow, ledger, clock, event_id, reviewer, DEATH, fields=change)
        health = next(f for f in record.harm_fields if f.name == "health_safety")
        purpose = evidence_purpose("health_safety")
        assert (
            health.status,
            opened(uow, event_id, purpose, health.evidence),
            health.confirmed_at,
        ) == (
            CONFIRMED,
            "patient harmed",
            clock.now(),
        )
        assert ledger.calls[0]["payload"]["harm_fields"]["health_safety"]["status"] == NA
        logged = ledger.calls[1]["payload"]["harm_fields"]
        assert logged == {"health_safety": {"status": "CONFIRMED", "evidence": health.evidence}}
        assert opened(uow, event_id, purpose, health.evidence) == "patient harmed"

    def test_failed_revision_keeps_the_previous_disposition(self, uow, clock, event_id, reviewer):
        ledger = RecordingLedger()
        dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
        ledger.fail_with = UnimplementedError("x", "REQ-06", "ledger")
        with pytest.raises(UnimplementedError):
            dispose(uow, ledger, clock, event_id, reviewer, NOT_SERIOUS, rationale="nope")
        assert uow.events.get(event_id).state == EventState.DISPOSITIONED
        assert [d.tier for d in uow.dispositions.for_event(event_id)] == ["OTHER_SERIOUS"]


def test_breach_without_harm_field_rows_can_still_be_dispositioned(
    uow, ledger, clock, event_id, reviewer
):
    """A breach flagged before migration 0005 has no rows: they're opened (unprefilled) first."""
    uow.harm_fields.rows.clear()
    record = dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
    assert [f.name for f in record.harm_fields] == [n.value for n in HarmFieldName]
    assert {f.prefill_evidence for f in record.harm_fields} == {None}
    assert len(uow.harm_fields.for_event(event_id)) == 4


def test_get_event_shows_the_revision_history(uow, ledger, clock, event_id, reviewer):
    dispose(uow, ledger, clock, event_id, reviewer, OTHER, fields=EX1_FIELDS)
    clock.advance(days=3)
    dispose(uow, ledger, clock, event_id, reviewer, DEATH)
    record = get_event(event_id, principal=user_principal(Role.VIEWER), uow=uow, ledger=ledger)
    history = EventDetail.from_record(record).model_dump(mode="json")["revision_history"]
    assert history == [
        {
            "tier": "OTHER_SERIOUS",
            "previous_tier": None,
            "reviewer_id": str(reviewer.actor_id),
            "recorded_at": "2026-09-27T20:01:00Z",
            "rationale": None,
            "deadline_utc": "2026-10-12T20:01:00Z",  # A + 15 days
        },
        {
            "tier": "DEATH",
            "previous_tier": "OTHER_SERIOUS",
            "reviewer_id": str(reviewer.actor_id),
            "recorded_at": "2026-09-30T20:01:00Z",
            "rationale": None,
            "deadline_utc": "2026-10-07T20:01:00Z",  # stricter: A + 10 days
        },
    ]


# --- HTTP ---------------------------------------------------------------------------------------

ALL_NA = {name.value: {"status": "NOT_APPLICABLE"} for name in HarmFieldName}


class Http:
    def __init__(self, uow, ledger, clock, principal):
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

    def post(self, event_id, body):
        return self.client.post(f"/v1/events/{event_id}/disposition", json=body)


@pytest.fixture
def http(uow, ledger, clock, reviewer) -> Http:
    return Http(uow, ledger, clock, reviewer)


class TestHttp:
    def test_disposition_returns_the_updated_event(self, http, event_id, reviewer):
        resp = http.post(
            event_id,
            {
                "tier": "OTHER_SERIOUS",
                "harm_fields": {
                    **ALL_NA,
                    "fundamental_rights": {"status": "CONFIRMED", "evidence": "credit denied"},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["event_id"] == str(event_id)
        assert body["state"] == "DISPOSITIONED"
        assert body["deadline_utc"] == "2026-10-12T20:01:00Z"  # B-10: A + 15 days
        rights = body["harm_fields"]["fundamental_rights"]
        assert rights["status"] == "CONFIRMED"
        assert rights["confirmed_by"] == str(reviewer.actor_id)
        assert rights["confirmed_at"] == "2026-09-27T20:01:00Z"
        assert body["harm_fields"]["health_safety"]["status"] == "NOT_APPLICABLE"
        assert [(r["tier"], r["previous_tier"]) for r in body["revision_history"]] == [
            ("OTHER_SERIOUS", None)
        ]

    def test_revision_over_http(self, http, event_id):
        assert http.post(event_id, {"tier": "OTHER_SERIOUS", "harm_fields": ALL_NA}).is_success
        resp = http.post(event_id, {"tier": "DEATH"})
        assert resp.status_code == 200, resp.text
        assert [(r["tier"], r["previous_tier"]) for r in resp.json()["revision_history"]] == [
            ("OTHER_SERIOUS", None),
            ("DEATH", "OTHER_SERIOUS"),
        ]

    def test_not_serious_closes(self, http, event_id):
        """Model 5 Ex-2 not-serious."""
        resp = http.post(
            event_id,
            {
                "tier": "NOT_SERIOUS",
                "rationale": "seasonal shift, no user impact",
                "harm_fields": ALL_NA,
            },
        )
        assert resp.status_code == 200, resp.text
        assert (resp.json()["state"], resp.json()["deadline_utc"]) == ("CLOSED", None)

    @pytest.mark.parametrize(
        "body",
        [
            {"tier": "NOT_SERIOUS", "harm_fields": ALL_NA},
            {"tier": "NOT_SERIOUS", "rationale": "", "harm_fields": ALL_NA},  # Model 5 Ex-2
            {"tier": "NOT_SERIOUS", "rationale": "  ", "harm_fields": ALL_NA},
            {"tier": "NOT_SERIOUS", "rationale": None, "harm_fields": ALL_NA},
            {},
            {"tier": "CRIT_INFRA"},  # Model 1 spelling, not the contract's (D-03)
            {"tier": "DEATH", "rationale": "x" * 4001},
            {"tier": "DEATH", "harm_fields": {"health_safety": {"status": "PENDING"}}},
            {"tier": "DEATH", "harm_fields": {"health_safety": {}}},
            {
                "tier": "DEATH",
                "harm_fields": {"health_safety": {"status": "CONFIRMED", "evidence": "x" * 4001}},
            },
            {"tier": "DEATH", "harm_fields": []},
        ],
    )
    def test_invalid_bodies_are_400_and_change_nothing(self, http, uow, ledger, event_id, body):
        resp = http.post(event_id, body)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # B-22 detail, plus the contract delta's Δ7 code (D-29).
        assert set(body) == {"detail", "error_code", "message"}
        assert body["error_code"] == "MALFORMED_BODY"
        assert_unchanged(uow, ledger, event_id)

    def test_malformed_json_is_400(self, http, event_id):
        resp = http.client.post(
            f"/v1/events/{event_id}/disposition",
            content=b"{not json",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
    def test_non_reviewers_are_403(self, http, uow, ledger, event_id, role):
        http.principal = user_principal(role)
        resp = http.post(event_id, {"tier": "DEATH", "harm_fields": ALL_NA})
        assert resp.status_code == 403
        assert isinstance(resp.json()["detail"], str)
        assert_refusal_recorded(ledger, event_id, http.principal, 403)
        assert_unchanged(uow, ledger, event_id)

    @pytest.mark.parametrize("event", ["unknown", "not-a-uuid"])
    def test_viewer_gets_403_whatever_the_id(self, http, event):
        http.principal = user_principal(Role.VIEWER)
        target = uuid4() if event == "unknown" else event
        assert http.post(target, {"tier": "DEATH"}).status_code == 403

    @pytest.mark.parametrize(
        "event_ref", [lambda: str(uuid4()), lambda: "not-a-uuid", lambda: str(uuid4()).upper()]
    )
    def test_unknown_or_non_canonical_id_is_404(self, http, event_ref):
        resp = http.post(event_ref(), {"tier": "DEATH", "harm_fields": ALL_NA})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "event not found"}

    def test_pending_fields_are_409(self, http, uow, ledger, event_id):
        resp = http.post(event_id, {"tier": "DEATH"})
        assert resp.status_code == 409
        assert "pending" in resp.json()["detail"]
        assert_refusal_recorded(ledger, event_id, http.principal, 409)
        assert_unchanged(uow, ledger, event_id)

    def test_refusals_of_unknown_events_are_not_recorded(self, http, ledger):
        http.principal = user_principal(Role.VIEWER)
        assert http.post(uuid4(), {"tier": "DEATH"}).status_code == 403
        assert http.post("not-a-uuid", {"tier": "DEATH"}).status_code == 403
        assert ledger.calls == []

    def test_a_failed_refusal_record_still_returns_the_refusal(self, http, ledger, event_id):
        """D-26: recording is best effort; the caller still gets its 409."""
        ledger.fail_with = RuntimeError("ledger down")
        assert http.post(event_id, {"tier": "DEATH"}).status_code == 409

    def test_gap_is_409(self, http, uow):
        uow.seed_system(SYSTEM)
        gap = Event(
            id=uuid4(),
            type=EventType.MONITORING_GAP.value,
            state=EventState.GAP_OPEN.value,
            system_id=SYSTEM,
            detected_at=T0,
            retain_until=retain_until_for(T0),
            gap_started_at=T0 - timedelta(minutes=15),
        )
        uow.events.add(gap)
        assert http.post(gap.id, {"tier": "DEATH", "harm_fields": ALL_NA}).status_code == 409

    def test_documented_in_openapi(self):
        op = create_app().openapi()["paths"]["/v1/events/{event_id}/disposition"]["post"]
        assert op["operationId"] == "recordDisposition"
        assert set(op["responses"]) >= {"200", "400", "401", "403", "404", "409"}

    def test_schema_states_the_rationale_rule(self):
        """B-08 is in the published schema, so schema-driven clients don't send a bare
        NOT_SERIOUS (the pod document uses the same allOf/if/then)."""
        schema = create_app().openapi()["components"]["schemas"]["DispositionIn"]
        (rule,) = schema["allOf"]
        assert rule["if"] == {"properties": {"tier": {"const": "NOT_SERIOUS"}}}
        assert rule["then"]["required"] == ["rationale"]
        assert rule["then"]["properties"]["rationale"]["pattern"] == r"\S"
