"""Phase 3 integrations on real PostgreSQL: ledger (REQ-06/14), auth (REQ-13), notifications
(REQ-04), gap detection (REQ-02), test routes, worker, and the Section 6.1 demonstration run end
to end over HTTP with the production wiring."""

import threading
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.core.clock import SystemClock, get_clock
from app.core.config import AppEnv, Settings
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role, hash_api_key
from app.modules.auth.service import create_user, revoke_key
from app.modules.events.models import Event, EventState, EventType
from app.modules.ledger.models import LedgerEntry
from app.modules.ledger.service import verify_ledger
from app.modules.ledger.writer import SqlLedgerWriter
from app.modules.metrics.models import MetricSample, MetricType
from app.modules.metrics.service import MetricSubmission, ingest_metric
from app.modules.monitoring_gaps.service import sweep_gaps
from app.modules.notifications.dispatcher import dispatch_due
from app.modules.notifications.models import DeliveryStatus, Notification, NotificationDelivery
from app.modules.notifications.outbox import OutboxNotifier
from app.modules.notifications.router import get_webhook_sender
from app.modules.systems.models import MonitoredSystem
from app.modules.testing.clock import SimulatedClock, get_simulated_clock
from app.modules.testing.fixtures import BASELINE_SYSTEMS
from app.modules.worker.runner import Worker, leadership, read_heartbeat, record_heartbeat
from tests.conftest import T0
from tests.fakes import FixedClock, RecordingSender, system_principal, user_principal

VIEWER = user_principal(Role.VIEWER)


@pytest.fixture
def clock():
    return FixedClock(T0 + timedelta(minutes=1))


@pytest.fixture
def ledger(clock):
    return SqlLedgerWriter(lambda: clock)


@pytest.fixture
def notifier(clock):
    return OutboxNotifier(lambda: clock, lambda: timedelta(minutes=15))


def count(sessionmaker, model) -> int:
    with sessionmaker() as s:
        return s.scalar(select(func.count()).select_from(model))


def entries(sessionmaker) -> list[LedgerEntry]:
    with sessionmaker() as s:
        return list(s.scalars(select(LedgerEntry).order_by(LedgerEntry.seq)))


def submit(make_uow, system_id, value, *, ledger, notifier, clock, sampled_at=T0):
    return ingest_metric(
        MetricSubmission(
            system_id=system_id,
            metric_name="drift_score",
            metric_type=MetricType.DRIFT,
            value=Decimal(value),
            sampled_at=sampled_at,
        ),
        principal=system_principal(system_id),
        uow=make_uow(),
        ledger=ledger,
        notifier=notifier,
        clock=clock,
    )


class TestLedgerOnPostgres:
    def test_breach_is_chained_and_verifies(self, make_uow, seed, ledger, notifier, clock):
        system_id, _, _ = seed()
        result = submit(make_uow, system_id, "0.35", ledger=ledger, notifier=notifier, clock=clock)
        verdict = verify_ledger(principal=VIEWER, uow=make_uow())
        assert verdict.valid and verdict.entries_checked == 1
        with make_uow() as uow:
            (entry,) = uow.ledger.for_event(result.flagged_event_id)
        assert entry.entry_type == "FLAGGED" and entry.at == clock.now()

    def test_payload_hash_survives_jsonb_round_trip(self, make_uow, ledger):
        payload = {
            "z": "last",
            "a": ["ü", "日本", " ", None, True, 2**62],
            "nested": {"k": {"deep": "x"}, "": "empty key"},
        }
        with make_uow() as uow:
            ledger.append(uow.session, entry_type="VIEW", actor_id=uuid.uuid4(), payload=payload)
        assert verify_ledger(principal=VIEWER, uow=make_uow()).valid

    def test_concurrent_appends_stay_linear(self, make_uow, pg_sessionmaker, clock):
        writer = SqlLedgerWriter(lambda: clock)
        barrier, errors = threading.Barrier(8), []

        def append():
            barrier.wait()
            try:
                with make_uow() as uow:
                    writer.append(uow.session, entry_type="VIEW", actor_id=None, payload={})
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=append) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert [e.seq for e in entries(pg_sessionmaker)] == list(range(1, 9))
        assert verify_ledger(principal=VIEWER, uow=make_uow()).valid

    def test_rolled_back_append_leaves_no_gap(self, make_uow, pg_sessionmaker, ledger):
        with pytest.raises(RuntimeError), make_uow() as uow:
            ledger.append(uow.session, entry_type="VIEW", actor_id=None, payload={})
            raise RuntimeError("caller failed after the append")
        with make_uow() as uow:
            ledger.append(uow.session, entry_type="VIEW", actor_id=None, payload={})
        assert [e.seq for e in entries(pg_sessionmaker)] == [1]

    @pytest.mark.parametrize(
        "tamper",
        [
            "UPDATE ledger_entry SET payload = '{\"edited\": true}' WHERE seq = 2",
            "UPDATE ledger_entry SET at = at + interval '1 second' WHERE seq = 2",
            "UPDATE ledger_entry SET actor_id = gen_random_uuid() WHERE seq = 2",
            "DELETE FROM ledger_entry WHERE seq = 2",
        ],
    )
    def test_ac06_direct_sql_tampering_is_detected(self, make_uow, pg_engine, ledger, tamper):
        """AC-06 / Ex-5: a DBA edits the ledger bypassing the app; the next pass detects it."""
        for _ in range(3):
            with make_uow() as uow:
                ledger.append(uow.session, entry_type="VIEW", actor_id=None, payload={"n": 1})
        assert verify_ledger(principal=VIEWER, uow=make_uow()).valid
        with pg_engine.begin() as conn:
            conn.execute(text(tamper))
        verdict = verify_ledger(principal=VIEWER, uow=make_uow())
        expected = 3 if tamper.startswith("DELETE") else 2
        assert (verdict.valid, verdict.first_mismatch) == (False, expected)


class TestAuthOnPostgres:
    def test_keys_resolve_and_revoke(self, make_uow, seed, clock):
        system_id, _, _ = seed()
        issued = create_user(make_uow(), name="sys", role=Role.SYSTEM, system_id=system_id)
        with make_uow() as uow:
            principal = uow.auth.principal_for_key(hash_api_key(issued.api_key))
        assert principal.actor_id == issued.user_id and principal.system_id == system_id
        revoke_key(make_uow(), key_id=issued.key_id, clock=clock)
        with make_uow() as uow:
            assert uow.auth.principal_for_key(hash_api_key(issued.api_key)) is None


class TestNotificationsOnPostgres:
    def test_outbox_row_commits_with_the_event(
        self, make_uow, seed, pg_sessionmaker, ledger, notifier, clock
    ):
        system_id, _, _ = seed()
        result = submit(make_uow, system_id, "0.35", ledger=ledger, notifier=notifier, clock=clock)
        with pg_sessionmaker() as s:
            (n,) = s.scalars(select(Notification))
        assert n.event_id == result.flagged_event_id
        assert n.deliver_by - n.created_at == timedelta(minutes=15)
        assert n.payload["observed_value"] == "0.35"

    def test_notifier_failure_stores_nothing(self, make_uow, seed, pg_sessionmaker, ledger, clock):
        system_id, _, _ = seed()

        class Broken(OutboxNotifier):
            def enqueue(self, session, **kwargs):
                super().enqueue(session, **kwargs)
                raise RuntimeError("after the insert")

        with pytest.raises(RuntimeError):
            submit(
                make_uow,
                system_id,
                "0.35",
                ledger=ledger,
                notifier=Broken(lambda: clock),
                clock=clock,
            )
        for model in (MetricSample, Event, Notification, LedgerEntry):
            assert count(pg_sessionmaker, model) == 0

    def test_dispatch_delivers_retries_and_records(
        self, make_uow, seed, pg_sessionmaker, ledger, notifier, clock
    ):
        from app.modules.notifications.models import Webhook

        system_id, _, _ = seed()
        with pg_sessionmaker.begin() as s:
            for i, url in enumerate(["https://good.example/", "https://bad.example/"]):
                s.add(
                    Webhook(
                        id=uuid.uuid4(),
                        url=url,
                        event_types=["flag_notification"],
                        secret=f"s{i}",
                        created_at=T0,
                    )
                )
        submit(make_uow, system_id, "0.35", ledger=ledger, notifier=notifier, clock=clock)
        sender = RecordingSender(
            respond=lambda url: __import__(
                "app.modules.notifications.delivery", fromlist=["x"]
            ).SendResult(
                "good" in url, 200 if "good" in url else 500, None if "good" in url else "HTTP 500"
            )
        )
        result = dispatch_due(make_uow, sender=sender, ledger=ledger, clock=clock, max_attempts=2)
        assert len(result.delivered) == 1 and result.failed_attempts == 1
        with pg_sessionmaker() as s:
            statuses = sorted(d.status for d in s.scalars(select(NotificationDelivery)))
        assert statuses == ["DELIVERED", "PENDING"]
        clock.advance(seconds=10)
        assert (
            dispatch_due(
                make_uow, sender=sender, ledger=ledger, clock=clock, max_attempts=2
            ).gave_up
            == 1
        )
        with pg_sessionmaker() as s:
            assert {d.status for d in s.scalars(select(NotificationDelivery))} == {
                DeliveryStatus.DELIVERED,
                DeliveryStatus.FAILED,
            }
        assert [e.entry_type for e in entries(pg_sessionmaker)] == ["FLAGGED", "NOTIFIED"]
        assert verify_ledger(principal=VIEWER, uow=make_uow()).valid


class TestGapsOnPostgres:
    def test_ex3_on_real_sql(self, make_uow, seed, pg_sessionmaker, ledger, notifier, clock):
        system_id, _, _ = seed()
        submit(make_uow, system_id, "0.1", ledger=ledger, notifier=notifier, clock=clock)
        last_seen = clock.now()
        clock.advance(minutes=15)
        (gap_id,) = sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=clock).opened
        assert sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=clock).opened == []

        clock.advance(minutes=85)  # data resumes 1 h 40 m after it stopped
        submit(
            make_uow,
            system_id,
            "0.1",
            ledger=ledger,
            notifier=notifier,
            clock=clock,
            sampled_at=T0 + timedelta(minutes=100),
        )
        clock.advance(seconds=5)
        assert sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=clock).closed == [
            gap_id
        ]
        with pg_sessionmaker() as s:
            gap = s.get(Event, gap_id)
        assert gap.state == EventState.GAP_CLOSED and gap.gap_started_at == last_seen
        assert gap.gap_ended_at - gap.gap_started_at == timedelta(minutes=100)
        assert [e.entry_type for e in entries(pg_sessionmaker)] == [
            "GAP_OPENED",
            "GAP_CLOSED",
        ]
        assert count(pg_sessionmaker, Notification) == 1

    def test_concurrent_sweepers_open_one_gap(
        self, make_uow, pg_sessionmaker, ledger, notifier, clock
    ):
        with pg_sessionmaker.begin() as s:
            s.add(MonitoredSystem(id=uuid.uuid4(), name="quiet", last_seen_at=T0))
        clock.current = T0 + timedelta(hours=1)
        barrier, results = threading.Barrier(4), []

        def sweeper():
            barrier.wait()
            results.append(sweep_gaps(make_uow, ledger=ledger, notifier=notifier, clock=clock))

        threads = [threading.Thread(target=sweeper) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(len(r.opened) for r in results) == 1
        assert count(pg_sessionmaker, Event) == 1 and count(pg_sessionmaker, Notification) == 1


class TestTestEnvironment:
    def test_reset_clears_state_keeps_accounts_and_seeds_fixtures(
        self, make_uow, seed, pg_sessionmaker, ledger, notifier, clock, monkeypatch
    ):
        from app.modules.testing import service

        system_id, _, _ = seed()
        issued = create_user(make_uow(), name="tester", role=Role.ADMIN)
        submit(make_uow, system_id, "0.35", ledger=ledger, notifier=notifier, clock=clock)
        SimulatedClock(pg_sessionmaker).advance(3600)
        monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=True))

        service.reset_test_environment(make_uow())

        for model in (Event, MetricSample, LedgerEntry, Notification):
            assert count(pg_sessionmaker, model) == 0
        with pg_sessionmaker() as s:
            assert s.get(MonitoredSystem, system_id).last_seen_at is None
            for fixture in BASELINE_SYSTEMS:
                assert s.get(MonitoredSystem, fixture.id).name == fixture.name
        with make_uow() as uow:
            assert uow.auth.principal_for_key(hash_api_key(issued.api_key)) is not None
            loan = BASELINE_SYSTEMS[0]
            accuracy = uow.thresholds.in_force(loan.id, "accuracy", T0)
            assert (accuracy.direction, accuracy.limit_value) == ("below", Decimal("0.70"))
        assert SimulatedClock(pg_sessionmaker).offset_seconds() == 0
        service.reset_test_environment(make_uow())  # idempotent

    def test_simulated_clock_is_shared_across_processes(self, pg_sessionmaker):
        api, worker = (
            SimulatedClock(pg_sessionmaker),
            SimulatedClock(pg_sessionmaker, cache_ttl_s=0),
        )
        before = worker.now()
        api.advance(900)
        assert worker.offset_seconds() == 900
        assert worker.now() - before >= timedelta(seconds=900)
        api.advance(60)
        assert worker.offset_seconds() == 960

    def test_heartbeat_and_single_active_worker(self, pg_engine, pg_sessionmaker):
        record_heartbeat(pg_sessionmaker, "host:1")
        record_heartbeat(pg_sessionmaker, "host:1")
        with pg_sessionmaker() as s:
            assert read_heartbeat(s) is not None
        stop = threading.Event()
        with leadership(pg_engine, stop, 0.05) as leader:
            assert leader is not None
            waiting = {}

            def second():
                with leadership(pg_engine, stop, 0.05) as conn:
                    waiting["conn"] = conn

            t = threading.Thread(target=second)
            t.start()
            t.join(0.3)
            assert t.is_alive()  # standing by while the first holds the lock
            stop.set()
            t.join(2)
            assert waiting["conn"] is None


def test_section_6_1_demonstration_over_http(pg_sessionmaker, pg_engine, seed, monkeypatch):
    """The Phase 3 demo, with the production auth, ledger and notifier on PostgreSQL:
    breach -> flagged event + webhook; withhold data -> gap event + webhook; audit trail;
    ledger verification before and after a deliberate tamper."""
    from app.core import clock as clock_module

    system_id, _, _ = seed(uuid.UUID("550e8400-e29b-41d4-a716-446655440000"))
    clock = FixedClock(T0 + timedelta(minutes=1))
    monkeypatch.setattr(clock_module, "_clock", clock)  # what the real ledger/notifier read
    make_uow = lambda: SqlAlchemyUnitOfWork(pg_sessionmaker)  # noqa: E731
    keys = {
        "system": create_user(make_uow(), name="sys", role=Role.SYSTEM, system_id=system_id),
        "viewer": create_user(make_uow(), name="viewer", role=Role.VIEWER),
        "admin": create_user(make_uow(), name="admin", role=Role.ADMIN),
    }
    sender = RecordingSender()
    app = create_app()
    app.dependency_overrides.update({get_uow: make_uow, get_webhook_sender: lambda: sender})
    client = TestClient(app)

    def as_(role):
        return {"Authorization": f"Bearer {keys[role].api_key}"}

    # The other team registers its webhook (verification ping included).
    hook = client.post(
        "/v1/webhooks",
        json={"url": "https://team-b.example/hook", "event_types": ["flag_notification"]},
        headers=as_("admin"),
    )
    assert hook.status_code == 201, hook.json()

    # 1. A snapshot above threshold -> flagged event -> webhook notification.
    metric = {
        "system_id": str(system_id),
        "metric_name": "drift_score",
        "metric_type": "drift",
        "value": 0.35,
        "timestamp": T0.isoformat(),
    }
    created = client.post("/v1/metrics", json=metric, headers=as_("system"))
    assert created.status_code == 201, created.json()
    event_id = created.json()["flagged_event_id"]

    from app.core.config import get_settings
    from app.modules.ledger.interface import get_ledger
    from app.modules.notifications.interface import get_notifier

    worker = Worker(
        uow_factory=make_uow,
        ledger=get_ledger(),
        notifier=get_notifier(),
        sender=sender,
        clock_provider=lambda: clock,
        max_attempts=get_settings().webhook_max_attempts,
    )
    clock.advance(seconds=5)
    assert worker.run_once().errors == []
    import json

    delivered = [json.loads(c["body"]) for c in sender.calls[1:]]  # [0] is the ping
    assert [d["event_id"] for d in delivered] == [event_id]

    detail = client.get(f"/v1/events/{event_id}", headers=as_("viewer"))
    assert detail.status_code == 200 and detail.json()["state"] == "FLAGGED"

    # 2. Withhold data for 3 intervals -> gap event -> webhook notification.
    clock.advance(minutes=15)
    worker.run_once()
    gaps = client.get("/v1/events?type=monitoring_gap", headers=as_("viewer")).json()
    assert gaps["total"] == 1 and gaps["items"][0]["state"] == "REVIEWER_NOTIFIED"
    gap_id = gaps["items"][0]["event_id"]
    assert json.loads(sender.calls[-1]["body"])["event"]["type"] == EventType.MONITORING_GAP

    # REQ-14: the audit trail shows who did what, including the view.
    audit = client.get(f"/v1/events/{event_id}/audit", headers=as_("viewer")).json()
    assert [(a["action"], a["actor_id"]) for a in audit] == [
        ("FLAGGED", str(keys["system"].user_id)),
        ("NOTIFIED", None),
        ("VIEW", str(keys["viewer"].user_id)),
    ]
    assert [
        a["action"] for a in client.get(f"/v1/events/{gap_id}/audit", headers=as_("viewer")).json()
    ] == [
        "GAP_OPENED",
        "NOTIFIED",
    ]

    # 3. Ledger verification before and after a deliberate tamper.
    assert client.get("/v1/ledger/verify", headers=as_("viewer")).json()["valid"] is True
    exported = client.get("/v1/ledger/export", headers=as_("viewer")).json()["entries"]
    with pg_engine.begin() as conn:
        conn.execute(text("UPDATE ledger_entry SET payload = '{}' WHERE seq = 2"))
    after = client.get("/v1/ledger/verify", headers=as_("viewer")).json()
    assert after["valid"] is False and after["first_mismatch"] == 2
    # The pre-tamper export still verifies offline; the post-tamper one doesn't.
    from tests.test_ledger import load_offline_verifier

    verifier = load_offline_verifier()
    assert verifier.verify(exported)["valid"] is True
    tampered = client.get("/v1/ledger/export", headers=as_("viewer")).json()["entries"]
    assert verifier.verify(tampered)["first_mismatch"] == 2

    # Roles: no anonymous access, viewers can't register webhooks, systems can't read.
    assert client.get("/v1/events").status_code == 401
    assert (
        client.post(
            "/v1/webhooks",
            json={"url": "https://x.example/", "event_types": ["reminder"]},
            headers=as_("viewer"),
        ).status_code
        == 403
    )
    assert client.get("/v1/ledger/verify", headers=as_("system")).status_code == 403


def test_test_routes_over_http(pg_sessionmaker, seed, monkeypatch):
    from app.modules.testing import service

    monkeypatch.setattr(service, "get_settings", lambda: Settings(enable_test_routes=True))
    make_uow = lambda: SqlAlchemyUnitOfWork(pg_sessionmaker)  # noqa: E731
    admin = create_user(make_uow(), name="admin", role=Role.ADMIN)
    sim = SimulatedClock(pg_sessionmaker, cache_ttl_s=0)
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    app.dependency_overrides.update({get_uow: make_uow, get_simulated_clock: lambda: sim})
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {admin.api_key}"}

    advanced = client.post("/v1/test/clock/advance", json={"seconds": 86400}, headers=headers)
    assert advanced.status_code == 200 and advanced.json()["offset_seconds"] == 86400
    assert client.post("/v1/test/reset", headers=headers).status_code == 204
    assert client.get("/v1/test/clock", headers=headers).json()["offset_seconds"] == 0
    # The admin key survived the reset; fixture systems exist.
    with pg_sessionmaker() as s:
        assert s.get(MonitoredSystem, BASELINE_SYSTEMS[0].id) is not None
    # Building the app does not swap the global clock; only the server lifespan installs it.
    assert isinstance(get_clock(), SystemClock)
