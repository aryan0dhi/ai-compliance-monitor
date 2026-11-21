"""POST /v1/events/{id}/disposition on PostgreSQL with the production auth and ledger
(REQ-08a/b, REQ-09, REQ-10, REQ-13; B-08 to B-10; Model 5 Ex-1 and Ex-2)."""

import threading
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role
from app.modules.auth.service import create_user
from app.modules.ledger.models import LedgerEntry
from app.modules.ledger.service import verify_ledger
from app.modules.records.service import evidence_purpose
from app.modules.review.disposition import (
    DispositionRequest,
    FieldConfirmation,
    record_disposition,
)
from app.modules.review.models import (
    Disposition,
    DispositionTier,
    HarmFieldName,
    HarmFieldStatus,
)
from tests.conftest import T0
from tests.fakes import FixedClock, opened, user_principal

ALL_NA = {name.value: {"status": "NOT_APPLICABLE"} for name in HarmFieldName}
EX1_FIELDS = {
    **ALL_NA,
    "fundamental_rights": {"status": "CONFIRMED", "evidence": "applicants denied credit"},
}


@pytest.fixture
def clock(monkeypatch):
    from app.core import clock as clock_module

    clock = FixedClock(T0 + timedelta(minutes=1))
    monkeypatch.setattr(clock_module, "_clock", clock)  # what the real ledger and routes read
    return clock


@pytest.fixture
def stack(pg_sessionmaker, seed, clock):
    system_id, _, _ = seed(harm_fields=["fundamental_rights"])
    make_uow = lambda: SqlAlchemyUnitOfWork(pg_sessionmaker)  # noqa: E731
    keys = {
        "system": create_user(make_uow(), name="sys", role=Role.SYSTEM, system_id=system_id),
        "viewer": create_user(make_uow(), name="viewer", role=Role.VIEWER),
        "reviewer": create_user(make_uow(), name="reviewer", role=Role.REVIEWER),
        "reviewer2": create_user(make_uow(), name="reviewer2", role=Role.REVIEWER),
    }
    app = create_app()
    app.dependency_overrides[get_uow] = make_uow
    client = TestClient(app)

    class Stack:
        def call(self, method, path, role, **kwargs):
            headers = {"Authorization": f"Bearer {keys[role].api_key}"}
            return client.request(method, path, headers=headers, **kwargs)

        def breach(self) -> str:
            metric = {
                "system_id": str(system_id),
                "metric_name": "accuracy",
                "metric_type": "performance",
                "value": 0.61,
                "timestamp": T0.isoformat(),
            }
            resp = self.call("POST", "/v1/metrics", "system", json=metric)
            assert resp.status_code == 201, resp.text
            return resp.json()["flagged_event_id"]

        def dispose(self, event_id, body, role="reviewer"):
            return self.call("POST", f"/v1/events/{event_id}/disposition", role, json=body)

        def audit(self, event_id):
            return self.call("GET", f"/v1/events/{event_id}/audit", "viewer").json()

    s = Stack()
    s.keys, s.make_uow, s.sessionmaker = keys, make_uow, pg_sessionmaker
    return s


def test_model5_ex1_assign_then_revise(stack, clock):
    """Ex-1: confirm fields, assign OTHER_SERIOUS, revise to DEATH three days later."""
    event_id = stack.breach()
    clock.advance(hours=4)
    first = stack.dispose(event_id, {"tier": "OTHER_SERIOUS", "harm_fields": EX1_FIELDS})
    assert first.status_code == 200, first.text
    assert first.json()["state"] == "DISPOSITIONED"
    rights = first.json()["harm_fields"]["fundamental_rights"]
    assert rights["status"] == "CONFIRMED"
    assert rights["confirmed_by"] == str(stack.keys["reviewer"].user_id)
    assert rights["prefill_evidence"].startswith("accuracy was 0.61")

    clock.advance(days=3)
    second = stack.dispose(event_id, {"tier": "DEATH"}, role="reviewer2")
    assert second.status_code == 200, second.text

    event = stack.call("GET", f"/v1/events/{event_id}", "viewer").json()
    assert event["state"] == "DISPOSITIONED"
    # B-10: A = d0 13:00 equivalent (T0 + 1 min + 4 h); DEATH is stricter, so A + 10 days.
    assert first.json()["deadline_utc"] == "2026-10-13T00:01:00Z"
    assert event["deadline_utc"] == "2026-10-08T00:01:00Z"
    assert [r["deadline_utc"] for r in event["revision_history"]] == [
        "2026-10-13T00:01:00Z",
        "2026-10-08T00:01:00Z",
    ]
    assert [
        (r["tier"], r["previous_tier"], r["reviewer_id"], r["recorded_at"])
        for r in event["revision_history"]
    ] == [
        ("OTHER_SERIOUS", None, str(stack.keys["reviewer"].user_id), "2026-09-28T00:01:00Z"),
        ("DEATH", "OTHER_SERIOUS", str(stack.keys["reviewer2"].user_id), "2026-10-01T00:01:00Z"),
    ]

    # B-14: one DISPOSITION entry per call, by the reviewer who made it; the chain verifies.
    actions = [(a["action"], a["actor_id"]) for a in stack.audit(event_id)]
    assert [a for a in actions if a[0] == "DISPOSITION"] == [
        ("DISPOSITION", str(stack.keys["reviewer"].user_id)),
        ("DISPOSITION", str(stack.keys["reviewer2"].user_id)),
    ]
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid


def test_model5_ex2_not_serious(stack):
    """Ex-2: a blank rationale is 400 and changes nothing; with one the event closes."""
    event_id = stack.breach()
    blank = stack.dispose(event_id, {"tier": "NOT_SERIOUS", "rationale": "", "harm_fields": ALL_NA})
    assert blank.status_code == 400
    assert stack.call("GET", f"/v1/events/{event_id}", "viewer").json()["state"] == "FLAGGED"

    closed = stack.dispose(
        event_id,
        {
            "tier": "NOT_SERIOUS",
            "rationale": "seasonal shift, no user impact",
            "harm_fields": ALL_NA,
        },
    )
    assert closed.status_code == 200, closed.text
    assert (closed.json()["state"], closed.json()["deadline_utc"]) == ("CLOSED", None)


def test_refusals_change_nothing_and_are_audited(stack):
    """A refused call changes nothing; each 403/409 on a real event adds one REFUSED entry
    attributed to the caller (D-26). Unknown ids add none."""
    event_id = stack.breach()
    before = stack.audit(event_id)
    assert (
        stack.dispose(event_id, {"tier": "DEATH", "harm_fields": ALL_NA}, "viewer").status_code
        == 403
    )
    assert stack.dispose(event_id, {"tier": "DEATH"}).status_code == 409  # fields pending
    partial = {k: v for k, v in EX1_FIELDS.items() if k != "health_safety"}
    assert stack.dispose(event_id, {"tier": "DEATH", "harm_fields": partial}).status_code == 409
    assert stack.dispose(str(uuid.uuid4()), {"tier": "DEATH"}).status_code == 404
    new = stack.audit(event_id)[len(before) :]
    assert [e["action"] for e in new] == ["REFUSED"] * 3
    assert [e["actor_id"] for e in new] == [
        str(stack.keys["viewer"].user_id),
        str(stack.keys["reviewer"].user_id),
        str(stack.keys["reviewer"].user_id),
    ]
    with stack.sessionmaker() as s:
        entries = s.scalars(
            select(LedgerEntry).where(LedgerEntry.entry_type == "REFUSED").order_by(LedgerEntry.seq)
        ).all()
        assert [(e.payload["operation"], e.payload["status"]) for e in entries] == [
            ("recordDisposition", 403),
            ("recordDisposition", 409),
            ("recordDisposition", 409),
        ]
        assert entries[0].payload["role"] == "viewer"
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid
    with stack.sessionmaker() as s:
        assert s.scalars(select(Disposition)).all() == []
    event = stack.call("GET", f"/v1/events/{event_id}", "viewer").json()
    assert {f["status"] for f in event["harm_fields"].values()} == {"PENDING"}


def test_concurrent_revisions_get_consecutive_numbers(stack, clock):
    """The event row lock serializes dispositions, so seq stays 1..n with no collision."""
    event_id = stack.breach()
    assert stack.dispose(event_id, {"tier": "OTHER_SERIOUS", "harm_fields": ALL_NA}).is_success
    reviewer = user_principal(Role.REVIEWER).model_copy(
        update={"actor_id": stack.keys["reviewer"].user_id}
    )
    from app.core.clock import get_clock
    from app.modules.ledger.interface import get_ledger

    barrier, errors = threading.Barrier(6), []

    def revise():
        barrier.wait()
        try:
            record_disposition(
                uuid.UUID(event_id),
                DispositionRequest(tier=DispositionTier.DEATH),
                principal=reviewer,
                uow=stack.make_uow(),
                ledger=get_ledger(),
                clock=get_clock(),
            )
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=revise) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with stack.sessionmaker() as s:
        rows = s.scalars(select(Disposition).order_by(Disposition.seq)).all()
        assert [d.seq for d in rows] == list(range(1, 8))
        assert [d.previous_tier for d in rows] == [None, "OTHER_SERIOUS"] + ["DEATH"] * 5
        entries = s.scalars(
            select(LedgerEntry).where(LedgerEntry.entry_type == "DISPOSITION")
        ).all()
        assert len(entries) == 7
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid


def test_service_confirmation_types_round_trip(stack):
    """Confirmation status and evidence are stored as given (direct service call)."""
    event_id = uuid.UUID(stack.breach())
    reviewer = user_principal(Role.REVIEWER).model_copy(
        update={"actor_id": stack.keys["reviewer"].user_id}
    )
    from app.core.clock import get_clock
    from app.modules.ledger.interface import get_ledger

    record_disposition(
        event_id,
        DispositionRequest(
            tier=DispositionTier.OTHER_SERIOUS,
            harm_fields={
                n: FieldConfirmation(HarmFieldStatus.NOT_APPLICABLE, "n/a: " + n.value)
                for n in HarmFieldName
            },
        ),
        principal=reviewer,
        uow=stack.make_uow(),
        ledger=get_ledger(),
        clock=get_clock(),
    )
    with stack.make_uow() as uow:
        fields = uow.harm_fields.for_event(event_id)
        # Stored sealed (REQ-16); opened with the event's key they round-trip exactly.
        assert [
            (f.status, opened(uow, event_id, evidence_purpose(f.name), f.evidence)) for f in fields
        ] == [("NOT_APPLICABLE", "n/a: " + n.value) for n in HarmFieldName]


def test_reminders_on_postgres_at_the_ac12_boundaries(stack, clock):
    """AC-12 / B-11 with the real worker, outbox and ledger: a reminder webhook gets one
    delivery at 50, 80 and 100 % of A..deadline and none at 49, 79 or 99 %."""
    import json

    from app.core.config import get_settings
    from app.modules.ledger.interface import get_ledger
    from app.modules.notifications.interface import get_notifier
    from app.modules.notifications.models import Notification, Webhook
    from app.modules.review.reminders import checkpoint
    from app.modules.worker.runner import Worker
    from tests.fakes import RecordingSender

    with stack.make_uow() as uow:
        uow.webhooks.add(
            Webhook(
                id=uuid.uuid4(),
                url="https://reviewer.example/reminders",
                event_types=["reminder"],
                secret="s",
                created_by=None,
                created_at=clock.now(),
                disabled_at=None,
            )
        )
    event_id = stack.breach()
    assert stack.dispose(event_id, {"tier": "DEATH", "harm_fields": ALL_NA}).is_success
    a = clock.now()
    deadline = a + timedelta(days=10)
    sender = RecordingSender()
    worker = Worker(
        uow_factory=stack.make_uow,
        ledger=get_ledger(),
        notifier=get_notifier(),
        sender=sender,
        clock_provider=lambda: clock,
        max_attempts=get_settings().webhook_max_attempts,
    )

    def reminders():
        return [json.loads(c["body"])["reminder"]["elapsed_percent"] for c in sender.calls]

    for percent, expected in [
        (49, []),
        (50, [50]),
        (79, [50]),
        (80, [50, 80]),
        (99, [50, 80]),
        (100, [50, 80, 100]),
    ]:
        clock.current = checkpoint(a, deadline, percent)
        if percent in (49, 79, 99):
            clock.current = checkpoint(a, deadline, percent + 1) - timedelta(seconds=1)
        assert worker.run_once().errors == []
        assert worker.run_once().errors == []  # a second pass at the same time adds nothing
        assert reminders() == expected, percent

    body = json.loads(sender.calls[-1]["body"])
    assert body["event_id"] == event_id
    assert body["reminder"]["deadline_utc"] == deadline.isoformat()
    with stack.sessionmaker() as s:
        rows = s.scalars(select(Notification).where(Notification.notification_type == "reminder"))
        assert sorted(n.reminder_percent for n in rows) == [50, 80, 100]
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid
