"""REQ-01 / REQ-03 services and HTTP endpoints running on the real SQL repositories.

Ledger and notifier are the explicit recording doubles (their modules aren't implemented yet);
everything else, including the unit of work and SQL, is production code.
"""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.clock import get_clock
from app.core.errors import UnimplementedError
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.events.models import Event, EventState
from app.modules.events.repository import EventFilters
from app.modules.events.service import get_event, list_events
from app.modules.ledger.interface import get_ledger
from app.modules.metrics.models import MetricSample, MetricType
from app.modules.metrics.service import (
    ConflictingDuplicateError,
    MetricSubmission,
    ingest_metric,
)
from app.modules.notifications.interface import get_notifier
from app.modules.systems.models import MonitoredSystem
from app.modules.thresholds.models import Direction
from app.modules.thresholds.service import ThresholdConfigError, add_threshold_version
from tests.conftest import T0
from tests.fakes import (
    FixedClock,
    RecordingLedger,
    RecordingNotifier,
    system_principal,
    user_principal,
)


def count(sessionmaker, model) -> int:
    with sessionmaker() as s:
        return s.scalar(select(func.count()).select_from(model))


def submit(make_uow, system_id, value, *, ledger=None, notifier=None, clock=None, **kw):
    return ingest_metric(
        MetricSubmission(
            system_id=system_id,
            metric_name=kw.get("metric_name", "drift_score"),
            metric_type=MetricType(kw.get("metric_type", "drift")),
            value=Decimal(value),
            sampled_at=kw.get("sampled_at", T0),
        ),
        principal=system_principal(system_id),
        uow=make_uow(),
        ledger=ledger or RecordingLedger(),
        notifier=notifier or RecordingNotifier(),
        clock=clock or FixedClock(T0 + timedelta(minutes=1)),
    )


class TestIngestion:
    def test_breach_persists_sample_and_event(self, make_uow, seed, pg_sessionmaker):
        system_id, drift_id, _ = seed()
        result = submit(make_uow, system_id, "0.35")
        with pg_sessionmaker() as s:
            sample = s.get(MetricSample, (system_id, "drift_score", T0))
            event = s.get(Event, result.flagged_event_id)
            system = s.get(MonitoredSystem, system_id)
        assert sample.value == Decimal("0.35") and sample.received_at == T0 + timedelta(minutes=1)
        assert event.state == "FLAGGED" and event.threshold_version_id == drift_id
        assert (event.observed_value, event.threshold_value) == (Decimal("0.35"), Decimal("0.30"))
        assert event.metric_timestamp == T0
        assert system.last_seen_at == T0 + timedelta(minutes=1)

    @pytest.mark.parametrize(
        ("metric", "mtype", "value", "flags"),
        [
            ("drift_score", "drift", "0.30", False),
            ("drift_score", "drift", "0.3", False),
            ("drift_score", "drift", "0.3000000001", True),
            ("accuracy", "performance", "0.70", False),
            ("accuracy", "performance", "0.6999999999", True),
        ],
    )
    def test_ac03_boundaries_with_numeric_column(self, make_uow, seed, metric, mtype, value, flags):
        system_id, _, _ = seed()
        result = submit(make_uow, system_id, value, metric_name=metric, metric_type=mtype)
        assert (result.flagged_event_id is not None) is flags

    def test_ledger_failure_persists_nothing(self, make_uow, seed, pg_sessionmaker):
        system_id, _, _ = seed()
        failing = RecordingLedger(fail_with=UnimplementedError("x", "REQ-06", "ledger"))
        with pytest.raises(UnimplementedError):
            submit(make_uow, system_id, "0.35", ledger=failing)
        assert count(pg_sessionmaker, MetricSample) == 0
        assert count(pg_sessionmaker, Event) == 0
        with pg_sessionmaker() as s:
            assert s.get(MonitoredSystem, system_id).last_seen_at is None

    def test_duplicate_handling_temporary_d04(self, make_uow, seed, pg_sessionmaker):
        system_id, _, _ = seed()
        first = submit(make_uow, system_id, "0.35")
        again = submit(make_uow, system_id, "0.350")
        assert not again.created and again.flagged_event_id == first.flagged_event_id
        with pytest.raises(ConflictingDuplicateError):
            submit(make_uow, system_id, "0.36")
        assert count(pg_sessionmaker, MetricSample) == 1
        assert count(pg_sessionmaker, Event) == 1

    def test_concurrent_identical_submissions_create_one_sample_and_event(
        self, make_uow, seed, pg_sessionmaker
    ):
        system_id, _, _ = seed()
        results, errors = [], []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                results.append(submit(make_uow, system_id, "0.35"))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert sum(r.created for r in results) == 1
        assert len({r.flagged_event_id for r in results}) == 1
        assert count(pg_sessionmaker, MetricSample) == 1
        assert count(pg_sessionmaker, Event) == 1


class TestThresholdConfig:
    def test_versions_append_and_apply_by_sample_time(self, make_uow, seed):
        system_id, _, _ = seed()
        add_threshold_version(
            make_uow(),
            system_id=system_id,
            metric_name="drift_score",
            metric_type=MetricType.DRIFT,
            direction=Direction.ABOVE,
            limit_value=Decimal("0.50"),
            valid_from=T0 + timedelta(hours=1),
            changed_by=None,
        )
        assert submit(make_uow, system_id, "0.40").flagged_event_id is not None  # vs 0.30
        late = submit(make_uow, system_id, "0.40", sampled_at=T0 + timedelta(hours=2))
        assert late.flagged_event_id is None  # vs 0.50

    def test_back_dated_version_rejected(self, make_uow, seed):
        system_id, _, _ = seed()
        with pytest.raises(ThresholdConfigError):
            add_threshold_version(
                make_uow(),
                system_id=system_id,
                metric_name="drift_score",
                metric_type=MetricType.DRIFT,
                direction=Direction.ABOVE,
                limit_value=Decimal("0.50"),
                valid_from=T0 - timedelta(days=2),
                changed_by=None,
            )


class TestEventQueries:
    @pytest.fixture
    def events(self, make_uow, seed):
        a, _, _ = seed()
        b, _, _ = seed()
        ids = []
        for i, sid in enumerate([a, a, b]):
            clock = FixedClock(T0 + timedelta(minutes=10 * (i + 1)))
            r = submit(make_uow, sid, "0.35", sampled_at=T0 + timedelta(minutes=i), clock=clock)
            ids.append(r.flagged_event_id)
        return a, b, ids

    def test_list_order_filters_pagination(self, make_uow, events):
        a, b, ids = events
        viewer = user_principal(Role.VIEWER)

        def page(**kw):
            limit = kw.pop("limit", 50)
            offset = kw.pop("offset", 0)
            return list_events(
                EventFilters(**kw), limit=limit, offset=offset, principal=viewer, uow=make_uow()
            )

        assert [e.id for e in page().items] == list(reversed(ids))
        assert page().total == 3
        assert {e.id for e in page(system_id=a).items} == set(ids[:2])
        assert page(state=EventState.CLOSED).total == 0
        assert [e.id for e in page(since=T0 + timedelta(minutes=20)).items] == [ids[2], ids[1]]
        p = page(limit=1, offset=1)
        assert [e.id for e in p.items] == [ids[1]] and p.total == 3

    def test_get_event_logs_view(self, make_uow, events):
        _, _, ids = events
        ledger = RecordingLedger()
        viewer = user_principal(Role.VIEWER)
        event = get_event(ids[0], principal=viewer, uow=make_uow(), ledger=ledger)
        assert event.id == ids[0]
        assert ledger.calls[0]["entry_type"] == "VIEW"
        assert ledger.calls[0]["session"] is not None  # a real session was passed


def test_http_end_to_end(pg_sessionmaker, seed):
    """POST /v1/metrics, then read it back, on real SQL. Only auth/ledger/notifier are doubled."""
    system_id, _, _ = seed(uuid.UUID("550e8400-e29b-41d4-a716-446655440000"))
    principal = {"p": system_principal(system_id)}
    app = create_app()
    app.dependency_overrides.update(
        {
            get_principal: lambda: principal["p"],
            get_uow: lambda: SqlAlchemyUnitOfWork(pg_sessionmaker),
            get_ledger: lambda: RecordingLedger(),
            get_notifier: lambda: RecordingNotifier(),
            get_clock: lambda: FixedClock(T0 + timedelta(minutes=1)),
        }
    )
    client = TestClient(app)
    body = {
        "system_id": str(system_id),
        "metric_name": "drift_score",
        "metric_type": "drift",
        "value": 0.35,
        "timestamp": "2026-09-27T20:00:00Z",
    }
    created = client.post("/v1/metrics", json=body)
    assert created.status_code == 201, created.json()
    event_id = created.json()["flagged_event_id"]

    principal["p"] = user_principal(Role.VIEWER)
    detail = client.get(f"/v1/events/{event_id}")
    assert detail.status_code == 200
    assert detail.json()["observed_value"] == 0.35
    assert detail.json()["retain_until"] == "2027-03-27T20:01:00Z"
    listing = client.get(f"/v1/events?system_id={system_id}")
    assert listing.json()["total"] == 1
