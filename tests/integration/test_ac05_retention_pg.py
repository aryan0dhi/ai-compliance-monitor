"""AC-05 (REQ-05, INV-10): an event and its metric data are retrievable at 5 months 29 days.

A breach and the samples around it are ingested at detection time, then the clock (a FixedClock
behind get_clock, the ledger and the notifier) is moved forward and the records are fetched: the
event through GET /v1/events[/{id}], the samples through the SQL metric repository (Section 5 has
no metric read endpoint). At 6 months 1 day the record may be present or purged; only a 5xx fails.

Testing limitation: this moves a mocked clock. Wall-clock retention (six real months) can't be
observed within the semester, and nothing purges data yet (Phase 5), so these checks guard the
future purge job rather than exercise one.

Checkpoints use calendar months clamped to month end, as retain_until_for does. For some
detection dates the literal 5mo29d falls after retain_until, on the same instant as 6mo1d
(docs/spec-decisions.md D-18); the event must still be retrievable there, as AC-05 says.
"""

import calendar
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.events.models import retain_until_for
from app.modules.ledger.interface import get_ledger
from app.modules.ledger.writer import SqlLedgerWriter
from app.modules.metrics.models import MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.notifications.interface import get_notifier
from app.modules.notifications.outbox import OutboxNotifier
from tests.fakes import FixedClock, system_principal, user_principal

SAMPLE_GAP = timedelta(minutes=5)


def plus(at: datetime, *, months: int, days: int) -> datetime:
    """at + calendar months (day clamped to month end, like retain_until_for) + days."""
    month_index = at.month - 1 + months
    year, month = at.year + month_index // 12, month_index % 12 + 1
    day = min(at.day, calendar.monthrange(year, month)[1])
    return at.replace(year=year, month=month, day=day) + timedelta(days=days)


@pytest.fixture
def clock():
    return FixedClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
def client(pg_sessionmaker, clock):
    app = create_app()
    app.dependency_overrides.update(
        {
            get_principal: lambda: user_principal(Role.VIEWER),
            get_uow: lambda: SqlAlchemyUnitOfWork(pg_sessionmaker),
            get_ledger: lambda: SqlLedgerWriter(lambda: clock),
            get_notifier: lambda: OutboxNotifier(lambda: clock, lambda: timedelta(minutes=15)),
            get_clock: lambda: clock,
        }
    )
    return TestClient(app)


def ingest_breach(make_uow, system_id, clock, detected: datetime):
    """Samples at detected - 5 min (normal), detected (breach) and detected + 5 min (normal)."""
    ledger = SqlLedgerWriter(lambda: clock)
    notifier = OutboxNotifier(lambda: clock, lambda: timedelta(minutes=15))
    flagged, sample_times = None, []
    for at, value in [
        (detected - SAMPLE_GAP, "0.10"),
        (detected, "0.35"),
        (detected + SAMPLE_GAP, "0.10"),
    ]:
        clock.current = at
        result = ingest_metric(
            MetricSubmission(
                system_id=system_id,
                metric_name="drift_score",
                metric_type=MetricType.DRIFT,
                value=Decimal(value),
                sampled_at=at,
            ),
            principal=system_principal(system_id),
            uow=make_uow(),
            ledger=ledger,
            notifier=notifier,
            clock=clock,
        )
        flagged = flagged or result.flagged_event_id
        sample_times.append(at)
    assert flagged is not None
    return str(flagged), sample_times


@pytest.mark.parametrize(
    ("detected", "at_5mo29d", "retain_until"),
    [
        # Mid-month: 5mo29d is two days inside the minimum.
        (
            datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
            datetime(2027, 4, 13, 12, 0, tzinfo=UTC),
            datetime(2027, 4, 15, 12, 0, tzinfo=UTC),
        ),
        # Month end: retain_until clamps Apr 31 to Apr 30; 5mo29d is the day before.
        (
            datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
            datetime(2027, 4, 29, 12, 0, tzinfo=UTC),
            datetime(2027, 4, 30, 12, 0, tzinfo=UTC),
        ),
        # Model 5 date: the fifth-to-sixth month spans February, so 5mo29d == 6mo1d (D-18).
        (
            datetime(2026, 9, 27, 20, 0, tzinfo=UTC),
            datetime(2027, 3, 28, 20, 0, tzinfo=UTC),
            datetime(2027, 3, 27, 20, 0, tzinfo=UTC),
        ),
    ],
    ids=["mid-month", "month-end", "model5-september"],
)
def test_ac05_event_and_metrics_retrievable_at_5mo29d(
    make_uow, seed, client, clock, detected, at_5mo29d, retain_until
):
    system_id, _, _ = seed()
    event_id, sample_times = ingest_breach(make_uow, system_id, clock, detected)
    assert plus(detected, months=5, days=29) == at_5mo29d
    assert retain_until_for(detected) == retain_until

    # Retrievable at 5mo29d (literal AC-05), the day before retain_until and at retain_until.
    for now in sorted({at_5mo29d, retain_until - timedelta(days=1), retain_until}):
        clock.current = now
        detail = client.get(f"/v1/events/{event_id}")
        assert detail.status_code == 200, (now, detail.text)
        e = detail.json()
        assert e["detected_at"] == detected.isoformat().replace("+00:00", "Z")
        assert e["retain_until"] == retain_until.isoformat().replace("+00:00", "Z")
        listed = client.get("/v1/events", params={"system_id": str(system_id)})
        assert listed.status_code == 200, listed.text
        assert event_id in {x["event_id"] for x in listed.json()["items"]}
        with make_uow() as uow:
            for sampled_at in sample_times:
                assert uow.metrics.get(system_id, "drift_score", sampled_at) is not None, now
            # The VIEW entry is stamped with the moved clock, so the read really happened then.
            assert uow.ledger.for_event(event_id)[-1].at == now

    # 6mo1d: present or purged are both acceptable (a minimum, not a maximum); no 5xx.
    clock.current = plus(detected, months=6, days=1)
    detail = client.get(f"/v1/events/{event_id}")
    assert detail.status_code in (200, 404), detail.text
    listed = client.get("/v1/events", params={"system_id": str(system_id)})
    assert listed.status_code == 200, listed.text
    with make_uow() as uow:
        for sampled_at in sample_times:
            uow.metrics.get(system_id, "drift_score", sampled_at)  # may be None; must not raise
