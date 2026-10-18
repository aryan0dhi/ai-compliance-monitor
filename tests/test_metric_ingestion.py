"""REQ-01 / REQ-03 business logic (metrics.service), using explicit test doubles."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.core.errors import UnimplementedError
from app.modules.auth.interface import Role
from app.modules.events.models import retain_until_for
from app.modules.ledger.interface import LedgerEntryType
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import (
    ConflictingDuplicateError,
    MetricSubmission,
    MetricTypeMismatchError,
    SystemScopeError,
    UnknownSystemError,
    ingest_metric,
)
from app.modules.notifications.interface import NotificationType
from tests.conftest import T0
from tests.fakes import system_principal, user_principal


def submit(uow, ledger, notifier, clock, *, principal=None, **overrides):
    system_id = overrides.pop("system_id")
    submission = MetricSubmission(
        system_id=system_id,
        metric_name=overrides.pop("metric_name", "drift_score"),
        metric_type=MetricType(overrides.pop("metric_type", "drift")),
        value=Decimal(overrides.pop("value", "0.10")),
        sampled_at=overrides.pop("sampled_at", T0),
    )
    assert not overrides, overrides
    return ingest_metric(
        submission,
        principal=principal or system_principal(system_id),
        uow=uow,
        ledger=ledger,
        notifier=notifier,
        clock=clock,
    )


@pytest.fixture
def system_id(uow):
    return uow.seed_system()


@pytest.fixture
def drift_threshold(uow, system_id):
    # Section 5.2 example: drift_score threshold 0.30.
    return uow.seed_threshold(
        system_id,
        "drift_score",
        metric_type="drift",
        direction="above",
        limit="0.30",
        valid_from=T0 - timedelta(days=1),
    )


@pytest.fixture
def accuracy_threshold(uow, system_id):
    # Model 5 Ex-1: accuracy threshold 0.70, breached when below.
    return uow.seed_threshold(
        system_id,
        "accuracy",
        metric_type="performance",
        direction="below",
        limit="0.70",
        valid_from=T0 - timedelta(days=1),
    )


class TestStorage:
    """REQ-01: samples are stored with server receive time; last_seen_at updated."""

    def test_stores_sample_without_threshold(self, uow, ledger, notifier, clock, system_id):
        result = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.99")
        assert result.created and result.flagged_event_id is None
        stored = uow.metrics.get(system_id, "drift_score", T0)
        assert stored.value == Decimal("0.99")
        assert stored.metric_type == "drift"
        assert stored.received_at == clock.now()
        assert uow.systems.get(system_id).last_seen_at == clock.now()
        assert uow.events.rows == {}
        assert ledger.calls == [] and notifier.calls == []
        assert uow.commits == 1

    def test_unknown_system(self, uow, ledger, notifier, clock):
        from uuid import uuid4

        with pytest.raises(UnknownSystemError):
            submit(uow, ledger, notifier, clock, system_id=uuid4())


class TestAuthorization:
    def test_key_for_other_system_rejected(self, uow, ledger, notifier, clock, system_id):
        other = uow.seed_system(name="resume-ranker")
        with pytest.raises(SystemScopeError):
            submit(
                uow, ledger, notifier, clock, system_id=system_id, principal=system_principal(other)
            )
        assert uow.metrics.rows == {}

    @pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER, Role.ADMIN])
    def test_human_keys_cannot_submit(self, uow, ledger, notifier, clock, system_id, role):
        with pytest.raises(SystemScopeError):
            submit(
                uow, ledger, notifier, clock, system_id=system_id, principal=user_principal(role)
            )


class TestThresholdDetectionAC03:
    """AC-03: drift and performance metrics; at-threshold must NOT flag; just past must flag."""

    @pytest.mark.parametrize(
        ("value", "flags"), [("0.35", True), ("0.30", False), ("0.3", False), ("0.29", False)]
    )
    def test_drift_above(
        self, uow, ledger, notifier, clock, system_id, drift_threshold, value, flags
    ):
        result = submit(uow, ledger, notifier, clock, system_id=system_id, value=value)
        assert (result.flagged_event_id is not None) is flags
        assert len(uow.events.rows) == (1 if flags else 0)

    @pytest.mark.parametrize(
        ("value", "flags"), [("0.61", True), ("0.70", False), ("0.7", False), ("0.71", False)]
    )
    def test_performance_below(
        self, uow, ledger, notifier, clock, system_id, accuracy_threshold, value, flags
    ):
        result = submit(
            uow,
            ledger,
            notifier,
            clock,
            system_id=system_id,
            metric_name="accuracy",
            metric_type="performance",
            value=value,
        )
        assert (result.flagged_event_id is not None) is flags

    def test_smallest_step_past_threshold_flags(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        result = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.3000000001")
        assert result.flagged_event_id is not None

    def test_event_records_all_req03_fields(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        result = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.35")
        event = uow.events.get(result.flagged_event_id)
        assert event.type == "threshold_breach"
        assert event.state == "FLAGGED"
        assert event.system_id == system_id
        assert event.metric_name == "drift_score"
        assert event.metric_timestamp == T0
        assert event.observed_value == Decimal("0.35")
        assert event.threshold_value == Decimal("0.30")
        assert event.threshold_version_id == drift_threshold.id
        assert event.detected_at == clock.now()  # REQ-15
        assert event.retain_until == retain_until_for(clock.now())  # REQ-05 storage
        assert event.evidence_bundle_hash is None  # D-05

    def test_uses_threshold_in_force_at_sample_time(self, uow, ledger, notifier, clock, system_id):
        uow.seed_threshold(
            system_id,
            "drift_score",
            metric_type="drift",
            direction="above",
            limit="0.30",
            valid_from=T0 - timedelta(days=2),
        )
        newer = uow.seed_threshold(
            system_id,
            "drift_score",
            metric_type="drift",
            direction="above",
            limit="0.50",
            valid_from=T0 + timedelta(hours=1),
        )
        # Sample taken before the 0.50 version took effect: judged against 0.30.
        early = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.40")
        assert early.flagged_event_id is not None
        # Sample taken after: judged against 0.50.
        late = submit(
            uow,
            ledger,
            notifier,
            clock,
            system_id=system_id,
            value="0.40",
            sampled_at=T0 + timedelta(hours=2),
        )
        assert late.flagged_event_id is None
        assert uow.events.get(early.flagged_event_id).threshold_version_id != newer.id

    def test_sample_before_any_threshold_never_flags(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        result = submit(
            uow,
            ledger,
            notifier,
            clock,
            system_id=system_id,
            value="0.99",
            sampled_at=T0 - timedelta(days=5),
        )
        assert result.flagged_event_id is None

    def test_metric_type_mismatch_rejected(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        with pytest.raises(MetricTypeMismatchError):
            submit(uow, ledger, notifier, clock, system_id=system_id, metric_type="performance")
        assert uow.metrics.rows == {}


class TestIntegrationsInSameTransaction:
    """Q7.2 Decision 1: event + ledger entry + notification commit together or not at all."""

    def test_breach_writes_ledger_and_notification(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        principal = system_principal(system_id)
        result = submit(
            uow, ledger, notifier, clock, system_id=system_id, value="0.35", principal=principal
        )
        assert len(ledger.calls) == 1 and len(notifier.calls) == 1
        entry, note = ledger.calls[0], notifier.calls[0]
        assert entry["entry_type"] == LedgerEntryType.FLAGGED
        assert entry["event_id"] == result.flagged_event_id
        assert entry["actor_id"] == principal.actor_id
        assert entry["payload"]["observed_value"] == "0.35"
        assert entry["payload"]["threshold_value"] == "0.30"
        assert note["notification_type"] == NotificationType.FLAG_NOTIFICATION
        assert note["event_id"] == result.flagged_event_id
        # Both received the unit of work's session (None for the fake).
        assert entry["session"] is uow.session and note["session"] is uow.session

    @pytest.mark.parametrize("failing", ["ledger", "notifier"])
    def test_integration_failure_rolls_back_everything(
        self, uow, ledger, notifier, clock, system_id, drift_threshold, failing
    ):
        error = UnimplementedError("x", "REQ-xx", failing)
        (ledger if failing == "ledger" else notifier).fail_with = error
        with pytest.raises(UnimplementedError):
            submit(uow, ledger, notifier, clock, system_id=system_id, value="0.35")
        assert uow.metrics.rows == {}
        assert uow.events.rows == {}
        assert uow.systems.get(system_id).last_seen_at is None
        assert uow.rollbacks == 1 and uow.commits == 0

    def test_production_integrations_block_breach_commit(
        self, uow, clock, system_id, drift_threshold
    ):
        """With the real (unimplemented) ledger and notifier, a breach cannot be committed."""
        from app.modules.ledger.interface import get_ledger
        from app.modules.notifications.interface import get_notifier

        with pytest.raises(UnimplementedError):
            submit(uow, get_ledger(), get_notifier(), clock, system_id=system_id, value="0.35")
        assert uow.metrics.rows == {} and uow.events.rows == {}

    def test_production_integrations_allow_non_breach(self, uow, clock, system_id, drift_threshold):
        """A non-breaching sample needs no ledger/notification, so it stores normally."""
        from app.modules.ledger.interface import get_ledger
        from app.modules.notifications.interface import get_notifier

        result = submit(uow, get_ledger(), get_notifier(), clock, system_id=system_id, value="0.1")
        assert result.created and result.flagged_event_id is None


class TestDuplicatesTemporaryD04:
    def test_identical_repeat_returns_original_without_new_event(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        first = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.35")
        clock.advance(minutes=5)
        again = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.350")
        assert not again.created
        assert again.flagged_event_id == first.flagged_event_id
        assert len(uow.events.rows) == 1
        assert len(ledger.calls) == 1 and len(notifier.calls) == 1
        # Original receive time and last_seen_at are untouched by the repeat.
        assert again.sample.received_at == first.sample.received_at
        assert uow.systems.get(system_id).last_seen_at == first.sample.received_at

    def test_identical_repeat_of_non_breach(self, uow, ledger, notifier, clock, system_id):
        submit(uow, ledger, notifier, clock, system_id=system_id, value="0.1")
        again = submit(uow, ledger, notifier, clock, system_id=system_id, value="0.1")
        assert not again.created and again.flagged_event_id is None

    def test_conflicting_repeat_rejected_and_original_kept(
        self, uow, ledger, notifier, clock, system_id, drift_threshold
    ):
        submit(uow, ledger, notifier, clock, system_id=system_id, value="0.10")
        with pytest.raises(ConflictingDuplicateError):
            submit(uow, ledger, notifier, clock, system_id=system_id, value="0.35")
        assert uow.metrics.get(system_id, "drift_score", T0).value == Decimal("0.10")
        assert uow.events.rows == {}
