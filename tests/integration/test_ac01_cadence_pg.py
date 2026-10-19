"""AC-01 on PostgreSQL: the received_at log read back from metric_sample keeps the 5-min cadence.

Same hour as tests/test_ac01_cadence.py, but through the SQL repositories, so timestamptz
storage and the gap sweeper's queries are the production ones.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.events.models import Event, EventType
from app.modules.metrics.models import MetricSample, MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.monitoring_gaps.service import sweep_gaps
from tests.conftest import T0
from tests.contract.cadence import CadenceViolation, cadence_violations
from tests.fakes import FixedClock, RecordingLedger, RecordingNotifier, system_principal

INTERVAL = timedelta(seconds=300)


def run_hour(make_uow, system_id, offsets: dict[int, timedelta]) -> None:
    ledger, notifier = RecordingLedger(), RecordingNotifier()
    arrivals = [T0 + i * INTERVAL + offsets.get(i, timedelta()) for i in range(13)]
    for at in arrivals:
        clock = FixedClock(at)
        sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=clock)
        ingest_metric(
            MetricSubmission(
                system_id=system_id,
                metric_name="drift_score",
                metric_type=MetricType.DRIFT,
                value=Decimal("0.1"),
                sampled_at=at,
            ),
            principal=system_principal(system_id),
            uow=make_uow(),
            ledger=ledger,
            notifier=notifier,
            clock=clock,
        )
    end = FixedClock(arrivals[-1] + INTERVAL)
    sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=end)


@pytest.mark.parametrize(
    ("offset", "violations"),
    [
        (timedelta(), []),
        (timedelta(seconds=10), []),
        (
            timedelta(seconds=11),
            [
                CadenceViolation(index=6, interval=INTERVAL + timedelta(seconds=11)),
                CadenceViolation(index=7, interval=INTERVAL - timedelta(seconds=11)),
            ],
        ),
    ],
    ids=["on-schedule", "10s-late", "11s-late"],
)
def test_ac01_received_at_log(make_uow, seed, pg_sessionmaker, offset, violations):
    system_id, _, _ = seed()
    run_hour(make_uow, system_id, {6: offset})
    with pg_sessionmaker() as s:
        log = s.scalars(
            select(MetricSample.received_at)
            .where(MetricSample.system_id == system_id)
            .order_by(MetricSample.received_at)
        ).all()
        gaps = s.scalars(select(Event.id).where(Event.type == EventType.MONITORING_GAP.value)).all()
    assert len(log) == 13
    assert cadence_violations(log) == violations
    assert gaps == []
