"""REQ-07 / B-07 / D-20: every breach event gets four PENDING Art. 3(49) fields at flag time.

A field the system lists (monitored_system.harm_fields) is prefilled with the breach's own facts;
the others are null. Monitoring gaps get none (INV-12).
"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from app.core.errors import UnimplementedError
from app.modules.auth.interface import Role
from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.events.schemas import EventDetail
from app.modules.events.service import get_event
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.review.models import HarmFieldName, HarmFieldStatus
from app.modules.review.service import breach_evidence
from app.modules.thresholds.models import ThresholdVersion
from tests.conftest import T0
from tests.fakes import RecordingNotifier, system_principal, user_principal

SYSTEM = UUID("550e8400-e29b-41d4-a716-446655440000")


def submit(uow, ledger, clock, value: str, *, notifier=None):
    return ingest_metric(
        MetricSubmission(
            system_id=SYSTEM,
            metric_name="accuracy",
            metric_type=MetricType.PERFORMANCE,
            value=Decimal(value),
            sampled_at=T0,
        ),
        principal=system_principal(SYSTEM),
        uow=uow,
        ledger=ledger,
        notifier=notifier or RecordingNotifier(),
        clock=clock,
    )


@pytest.fixture
def threshold_id(uow) -> UUID:
    uow.seed_system(SYSTEM, harm_fields=["fundamental_rights"])
    return uow.seed_threshold(
        SYSTEM,
        "accuracy",
        metric_type="performance",
        direction="below",
        limit="0.70",
        valid_from=T0 - timedelta(days=1),
    ).id


def test_breach_opens_all_four_fields_pending(uow, ledger, clock, threshold_id):
    event_id = submit(uow, ledger, clock, "0.61").flagged_event_id
    fields = uow.harm_fields.for_event(event_id)
    assert [f.name for f in fields] == [h.value for h in HarmFieldName]
    assert {f.status for f in fields} == {HarmFieldStatus.PENDING}
    assert {(f.confirmed_by, f.confirmed_at, f.evidence) for f in fields} == {(None, None, None)}


def test_only_the_systems_fields_are_prefilled_from_the_breach(uow, ledger, clock, threshold_id):
    """Model 5 Ex-1: loan-scorer accuracy 0.61 prefills fundamental_rights, others blank."""
    event_id = submit(uow, ledger, clock, "0.61").flagged_event_id
    prefill = {f.name: f.prefill_evidence for f in uow.harm_fields.for_event(event_id)}
    assert prefill == {
        "health_safety": None,
        "critical_infrastructure": None,
        "fundamental_rights": "accuracy was 0.61 at 2026-09-27T20:00:00Z, below the threshold of "
        f"0.70 (threshold version {threshold_id}).",
        "property_environment": None,
    }


def test_no_breach_no_fields(uow, ledger, clock, threshold_id):
    assert submit(uow, ledger, clock, "0.90").flagged_event_id is None
    assert uow.harm_fields.rows == {}


def test_fields_roll_back_with_the_event(uow, ledger, clock, threshold_id):
    """Same transaction as the event: if the notification can't be queued, no fields remain."""
    failing = RecordingNotifier(fail_with=UnimplementedError("x", "REQ-04", "notifications"))
    with pytest.raises(UnimplementedError):
        submit(uow, ledger, clock, "0.61", notifier=failing)
    assert uow.events.rows == {} and uow.harm_fields.rows == {}


def test_get_event_returns_the_fields(uow, ledger, clock, threshold_id):
    event_id = submit(uow, ledger, clock, "0.61").flagged_event_id
    record = get_event(event_id, principal=user_principal(Role.REVIEWER), uow=uow, ledger=ledger)
    detail = EventDetail.from_record(record)
    assert detail.harm_fields is not None
    assert list(detail.harm_fields) == [h.value for h in HarmFieldName]
    assert detail.harm_fields["fundamental_rights"].prefill_evidence is not None


def test_gap_event_has_null_harm_fields(uow, ledger):
    """INV-12: a monitoring gap never gets harm fields; the API shows null."""
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
    record = get_event(gap.id, principal=user_principal(Role.VIEWER), uow=uow, ledger=ledger)
    assert record.harm_fields == []
    assert EventDetail.from_record(record).harm_fields is None


def test_evidence_uses_utc_and_the_stored_values():
    tv = ThresholdVersion(id=uuid4(), direction="above")
    event = Event(
        metric_name="psi",
        observed_value=Decimal("0.34"),
        threshold_value=Decimal("0.30"),
        threshold_version_id=tv.id,
        metric_timestamp=T0.astimezone(ZoneInfo("America/New_York")),
    )
    assert breach_evidence(event, tv) == (
        "psi was 0.34 at 2026-09-27T20:00:00Z, above the threshold of 0.30 "
        f"(threshold version {tv.id})."
    )
