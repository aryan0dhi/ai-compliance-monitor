"""Article 73 deadlines (REQ-10, REQ-15; B-10, B-15; AC-11, AC-17; D-23)."""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.events.schemas import EventDetail
from app.modules.ledger.interface import get_ledger
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.interface import get_notifier
from app.modules.review.deadlines import (
    DEADLINE_DAYS,
    DeadlineState,
    LooserTierNeedsRationale,
    apply,
    deadline_for,
    replay,
    strictness,
)
from app.modules.review.disposition import (
    DispositionConflict,
    DispositionRequest,
    FieldConfirmation,
    record_disposition,
)
from app.modules.review.models import DispositionTier, HarmFieldName, HarmFieldStatus
from tests.conftest import T0
from tests.fakes import RecordingNotifier, system_principal, user_principal

DEATH = DispositionTier.DEATH
CRIT = DispositionTier.CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD
OTHER = DispositionTier.OTHER_SERIOUS
NOT = DispositionTier.NOT_SERIOUS
A = T0
DAY = timedelta(days=1)


def test_days_per_tier():
    """B-10: DEATH 10, CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD 2, OTHER_SERIOUS 15; exactly 24 h."""
    assert {t: deadline_for(A, t) for t in DispositionTier} == {
        DEATH: A + timedelta(hours=240),
        CRIT: A + timedelta(hours=48),
        OTHER: A + timedelta(hours=360),
        NOT: None,
    }
    assert sorted(DispositionTier, key=strictness) == [CRIT, DEATH, OTHER, NOT]


class TestFirstDisposition:
    @pytest.mark.parametrize("tier", [DEATH, CRIT, OTHER])
    def test_serious_tier_sets_a_and_the_deadline(self, tier):
        state = apply(DeadlineState(), tier, rationale=False, at=A)
        assert state == DeadlineState(tier=tier, anchor=A, deadline=A + DEADLINE_DAYS[tier] * DAY)

    def test_not_serious_sets_neither(self):
        assert apply(DeadlineState(), NOT, rationale=True, at=A) == DeadlineState(tier=NOT)


class TestRevisions:
    """Model 5 Ex-1 shape: A at d0, revisions on later days."""

    def test_stricter_takes_the_earlier_deadline(self):
        """Ex-1: OTHER_SERIOUS at A, DEATH at A+3d → A+10d (not d3+10d)."""
        state = replay([(OTHER, False, A), (DEATH, False, A + 3 * DAY)])
        assert (state.anchor, state.deadline) == (A, A + 10 * DAY)

    def test_stricter_never_moves_the_deadline_later(self):
        """DEATH at A, then CRITICAL a day later: A + 2 d, the earlier of the two."""
        state = replay([(DEATH, False, A), (CRIT, False, A + DAY)])
        assert state.deadline == A + 2 * DAY

    def test_looser_without_rationale_is_refused(self):
        state = replay([(DEATH, False, A)])
        with pytest.raises(LooserTierNeedsRationale, match="DEATH to the less strict OTHER"):
            apply(state, OTHER, rationale=False, at=A + DAY)

    def test_looser_with_rationale_uses_a(self):
        state = replay([(DEATH, False, A), (OTHER, True, A + 5 * DAY)])
        assert (state.anchor, state.deadline) == (A, A + 15 * DAY)

    def test_not_serious_clears_and_a_survives(self):
        """DEATH → NOT_SERIOUS (rationale) → DEATH: back to A + 10 d, A unchanged."""
        state = replay([(DEATH, False, A), (NOT, True, A + DAY)])
        assert (state.anchor, state.deadline) == (A, None)
        state = apply(state, DEATH, rationale=False, at=A + 4 * DAY)
        assert (state.anchor, state.deadline) == (A, A + 10 * DAY)

    def test_not_serious_first_then_serious_anchors_at_that_time(self):
        state = replay([(NOT, True, A), (OTHER, False, A + 2 * DAY)])
        assert (state.anchor, state.deadline) == (A + 2 * DAY, A + 17 * DAY)

    def test_same_tier_keeps_the_deadline(self):
        state = replay([(OTHER, False, A), (OTHER, False, A + 3 * DAY)])
        assert state.deadline == A + 15 * DAY


@settings(max_examples=300, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(list(DispositionTier)),
            st.booleans(),
            st.integers(min_value=0, max_value=20 * 24),
        ),
        min_size=1,
        max_size=12,
    )
)
def test_rules_hold_for_any_sequence(steps):
    """A never moves once set; the deadline is always A + days(some tier) and null exactly for
    NOT_SERIOUS; a stricter tier never moves it later; only a rationale-less looser revision is
    refused, and a refusal changes nothing."""
    state, at = DeadlineState(), A
    for tier, rationale, hours in steps:
        at += timedelta(hours=hours)
        rationale = rationale or tier is NOT  # NOT_SERIOUS always carries one (B-08)
        looser = state.tier is not None and strictness(tier) > strictness(state.tier)
        if looser and not rationale:
            with pytest.raises(LooserTierNeedsRationale):
                apply(state, tier, rationale=rationale, at=at)
            continue
        new = apply(state, tier, rationale=rationale, at=at)
        if state.anchor is not None:
            assert new.anchor == state.anchor
        assert (new.deadline is None) == (tier is NOT)
        if new.deadline is not None:
            assert new.anchor is not None
            assert new.deadline - new.anchor in {d * DAY for d in DEADLINE_DAYS.values()}
            assert new.deadline <= new.anchor + DEADLINE_DAYS[tier] * DAY
        stricter = state.tier is not None and strictness(tier) < strictness(state.tier)
        if stricter and state.deadline is not None:
            assert new.deadline is not None and new.deadline <= state.deadline
        state = new


# --- through the service and HTTP ----------------------------------------------------------------

SYSTEM = UUID("550e8400-e29b-41d4-a716-446655440000")
ALL_NA = {name: FieldConfirmation(HarmFieldStatus.NOT_APPLICABLE) for name in HarmFieldName}


@pytest.fixture
def event_id(uow, ledger, clock) -> UUID:
    uow.seed_system(SYSTEM)
    uow.seed_threshold(
        SYSTEM,
        "accuracy",
        metric_type="performance",
        direction="below",
        limit="0.70",
        valid_from=T0 - DAY,
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
    return result.flagged_event_id


def dispose(uow, ledger, clock, event_id, tier, rationale=None):
    return record_disposition(
        event_id,
        DispositionRequest(tier=tier, rationale=rationale, harm_fields=ALL_NA),
        principal=user_principal(Role.REVIEWER),
        uow=uow,
        ledger=ledger,
        clock=clock,
    )


def test_ex1_deadline_through_the_service(uow, ledger, clock, event_id):
    """AC-11 / Ex-1: OTHER_SERIOUS at d0 13:00 (A), DEATH at d3 → A + 10 d; each revision
    records the deadline it set, and detected_at never changes (AC-17, B-15)."""
    detected = uow.events.get(event_id).detected_at
    clock.advance(hours=4)
    a = clock.now()
    first = dispose(uow, ledger, clock, event_id, OTHER)
    assert EventDetail.from_record(first).deadline_utc == a + 15 * DAY
    clock.advance(days=3)
    detail = EventDetail.from_record(dispose(uow, ledger, clock, event_id, DEATH))
    assert detail.deadline_utc == a + 10 * DAY
    assert [r.deadline_utc for r in detail.revision_history] == [a + 15 * DAY, a + 10 * DAY]
    assert detail.detected_at == detected
    assert ledger.calls[-1]["payload"]["deadline_utc"] == (a + 10 * DAY).isoformat()


def test_looser_revision_without_rationale_is_409_and_changes_nothing(uow, ledger, clock, event_id):
    dispose(uow, ledger, clock, event_id, DEATH)
    calls = len(ledger.calls)
    with pytest.raises(DispositionConflict, match="needs a rationale"):
        dispose(uow, ledger, clock, event_id, OTHER, rationale="   ")
    assert [d.tier for d in uow.dispositions.for_event(event_id)] == ["DEATH"]
    assert len(ledger.calls) == calls


def test_looser_revision_over_http(uow, ledger, clock, event_id):
    app = create_app()
    app.dependency_overrides.update(
        {
            get_principal: lambda: user_principal(Role.REVIEWER),
            get_uow: lambda: uow,
            get_ledger: lambda: ledger,
            get_notifier: lambda: RecordingNotifier(),
            get_clock: lambda: clock,
        }
    )
    client = TestClient(app)
    url = f"/v1/events/{event_id}/disposition"
    na = {name.value: {"status": "NOT_APPLICABLE"} for name in HarmFieldName}
    assert (
        client.post(
            url, json={"tier": "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "harm_fields": na}
        ).json()["deadline_utc"]
        == "2026-09-29T20:01:00Z"
    )
    refused = client.post(url, json={"tier": "OTHER_SERIOUS"})
    assert refused.status_code == 409 and "rationale" in refused.json()["detail"]
    clock.advance(days=1)
    looser = client.post(url, json={"tier": "OTHER_SERIOUS", "rationale": "no outage after all"})
    assert looser.status_code == 200, looser.text
    body = looser.json()
    assert body["deadline_utc"] == "2026-10-12T20:01:00Z"  # A + 15 d, A unchanged
    assert body["detected_at"] == "2026-09-27T20:01:00Z"
    assert [r["deadline_utc"] for r in body["revision_history"]] == [
        "2026-09-29T20:01:00Z",
        "2026-10-12T20:01:00Z",
    ]
