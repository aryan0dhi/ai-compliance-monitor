"""REQ-16 / AC-18 on PostgreSQL: personal data is never stored in clear, erasure at the end of
retention leaves every ledger hash intact, and migration 0012 seals rows that existed before it."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.db.base import Base
from app.db.session import create_app_sessionmaker
from app.db.uow import SqlAlchemyUnitOfWork, get_uow
from app.main import create_app
from app.modules.auth.interface import Role
from app.modules.auth.service import create_user
from app.modules.ledger.interface import get_ledger
from app.modules.ledger.models import LedgerEntry
from app.modules.ledger.service import verify_ledger
from app.modules.records.crypto import is_token
from app.modules.records.models import RecordKey
from app.modules.records.service import erase_expired, erasure_due_at
from app.modules.review.models import Disposition, HarmField, Report
from tests.conftest import T0
from tests.fakes import user_principal
from tests.integration.conftest import migrate, throwaway_database
from tests.integration.test_report_pg import (  # noqa: F401 - fixtures
    EX1_FIELDS,
    clock,
    stack,
    submission_body,
)

RATIONALE = "spoke with the applicant, Ms Example, on 2026-09-28"
EVIDENCE = "applicant Mr Sample was refused credit twice"
# A Phase 4 submission note (POST .../report/submission no longer takes one since contract
# delta Δ3); only the 0012 migration test still writes one, straight into the table.
NOTE = "submitted by J. Doe via the authority's portal"
SECRETS = (RATIONALE, EVIDENCE)
FIELDS = {**EX1_FIELDS, "fundamental_rights": {"status": "CONFIRMED", "evidence": EVIDENCE}}


def everything_in_the_database(sessionmaker) -> str:
    """Every row of every table as text, to search for anything stored in clear."""
    with sessionmaker() as s:
        return "\n".join(
            repr(row) for table in Base.metadata.sorted_tables for row in s.execute(table.select())
        )


def full_event(stack) -> str:  # noqa: F811
    event_id = stack.breach()
    stack.dispose(
        event_id, {"tier": "OTHER_SERIOUS", "rationale": RATIONALE, "harm_fields": FIELDS}
    )
    assert stack.report(event_id).status_code == 200
    body = submission_body(stack, event_id, authority_reference="MSA-2026-0042")
    assert stack.submit(event_id, json=body).status_code == 201
    return event_id


def test_ac18_erasure_keeps_every_hash_and_the_chain_verifies(stack, clock):  # noqa: F811
    event_id = full_event(stack)
    event = stack.call("GET", f"/v1/events/{event_id}", "viewer").json()
    assert event["revision_history"][0]["rationale"] == RATIONALE  # readable before

    dump = everything_in_the_database(stack.sessionmaker)
    for secret in SECRETS:
        assert secret not in dump, secret
    with stack.sessionmaker() as s:
        before = [(e.seq, e.entry_hash, e.payload) for e in s.scalars(select(LedgerEntry))]
        assert all(is_token(v) for v in s.scalars(select(Disposition.rationale)))
        assert is_token(s.scalars(select(Report.document)).one())

    # AC-18: past retention, the worker's job destroys the key.
    with stack.make_uow() as uow:
        due = erasure_due_at(uow.events.get(uuid.UUID(event_id)))
    clock.advance(seconds=(due - clock.now()).total_seconds())
    erased = erase_expired(stack.make_uow, ledger=get_ledger(), clock=clock)
    assert erased.erased == [uuid.UUID(event_id)]

    with stack.sessionmaker() as s:
        after = [(e.seq, e.entry_hash, e.payload) for e in s.scalars(select(LedgerEntry))]
        key = s.get(RecordKey, uuid.UUID(event_id))
        assert (key.wrapped_key, key.destroyed_at) == (None, due)
    # Positions and hashes of every earlier entry are unchanged; one entry was appended.
    assert after[: len(before)] == before
    assert [p["event_id"] for _, _, p in after[len(before) :]] == [event_id]
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid

    event = stack.call("GET", f"/v1/events/{event_id}", "viewer").json()
    assert event["revision_history"][0]["rationale"] is None
    assert event["personal_data_erased_at"] == due.isoformat().replace("+00:00", "Z")
    assert [a["action"] for a in stack.audit(event_id)][-2:] == ["RECORD_ERASED", "VIEW"]
    assert stack.report(event_id).status_code == 410  # contract delta Δ2


def test_erasure_runs_on_the_service_clock_only_after_retention(stack, clock):  # noqa: F811
    event_id = full_event(stack)
    with stack.make_uow() as uow:
        event = uow.events.get(uuid.UUID(event_id))
        retain_until, due = event.retain_until, erasure_due_at(event)
    assert due - retain_until == timedelta(days=1)  # minimal retention: AC-05's extra day
    clock.advance(seconds=(retain_until - clock.now()).total_seconds())
    assert erase_expired(stack.make_uow, ledger=get_ledger(), clock=clock).erased == []
    assert stack.report(event_id).status_code == 200
    clock.advance(days=1)
    assert erase_expired(stack.make_uow, ledger=get_ledger(), clock=clock).erased == [
        uuid.UUID(event_id)
    ]


@pytest.fixture
def scratch_db(postgres_server_url):
    with throwaway_database(postgres_server_url) as engine:
        yield engine


def test_0012_seals_existing_rows_and_downgrade_opens_them(scratch_db, monkeypatch):
    """Rows written before 0012 hold plaintext: upgrading seals them in place, and the API still
    reads them. Simulated by downgrading data written at head, which opens every value."""
    from app.core import clock as clock_module
    from tests.fakes import FixedClock

    migrate(scratch_db)
    sessionmaker = create_app_sessionmaker(scratch_db)
    monkeypatch.setattr(clock_module, "_clock", FixedClock(T0 + timedelta(minutes=1)))

    system_id = uuid.uuid4()
    with sessionmaker.begin() as s:
        s.execute(
            text(
                "INSERT INTO monitored_system (id, name, sampling_interval_s, harm_fields) "
                "VALUES (:id, 'sys', 300, ARRAY['fundamental_rights'])"
            ),
            {"id": system_id},
        )
        s.execute(
            text(
                "INSERT INTO threshold_version (id, system_id, metric_name, metric_type, "
                "direction, limit_value, valid_from) VALUES (:id, :s, 'accuracy', 'performance', "
                "'below', 0.70, :t)"
            ),
            {"id": uuid.uuid4(), "s": system_id, "t": T0 - timedelta(days=1)},
        )
    make_uow = lambda: SqlAlchemyUnitOfWork(sessionmaker)  # noqa: E731
    keys = {
        "system": create_user(make_uow(), name="sys", role=Role.SYSTEM, system_id=system_id),
        "reviewer": create_user(make_uow(), name="reviewer", role=Role.REVIEWER),
    }
    app = create_app()
    app.dependency_overrides[get_uow] = make_uow
    client = TestClient(app)

    def call(method, path, role, **kwargs):
        headers = {"Authorization": f"Bearer {keys[role].api_key}"}
        return client.request(method, path, headers=headers, **kwargs)

    metric = {
        "system_id": str(system_id),
        "metric_name": "accuracy",
        "metric_type": "performance",
        "value": 0.61,
        "timestamp": T0.isoformat(),
    }
    event_id = call("POST", "/v1/metrics", "system", json=metric).json()["flagged_event_id"]
    body = {"tier": "OTHER_SERIOUS", "rationale": RATIONALE, "harm_fields": FIELDS}
    assert call("POST", f"/v1/events/{event_id}/disposition", "reviewer", json=body).is_success
    report = call("GET", f"/v1/events/{event_id}/report", "reviewer")

    migrate(scratch_db, "0011", downgrade=True)
    with scratch_db.begin() as conn:
        assert conn.execute(text("SELECT rationale FROM disposition")).scalar_one() == RATIONALE
        # A Phase 4 submission with a note, as rows written before 0012 could hold.
        conn.execute(
            text("UPDATE report SET submitted_at = now(), submitted_by = :u, submission_note = :n"),
            {"u": keys["reviewer"].user_id, "n": NOTE},
        )
        assert (
            conn.execute(
                text("SELECT evidence FROM harm_field WHERE name = 'fundamental_rights'")
            ).scalar_one()
            == EVIDENCE
        )
        assert conn.execute(text("SELECT to_regclass('record_key')")).scalar_one() is None

    migrate(scratch_db)  # 0012 seals the rows, 0017 gives the existing report an id
    dump = everything_in_the_database(sessionmaker)
    for secret in (*SECRETS, NOTE):
        assert secret not in dump, secret
    with sessionmaker() as s:
        row = s.scalars(select(Report)).one()
        assert row.id is not None and is_token(row.submission_note)
    with sessionmaker() as s:
        assert s.scalars(select(RecordKey.event_id)).all() == [uuid.UUID(event_id)]
        assert all(is_token(f.evidence) for f in s.scalars(select(HarmField)) if f.evidence)
    # The sealed rows read exactly as before through the API.
    detail = call("GET", f"/v1/events/{event_id}", "reviewer").json()
    assert detail["revision_history"][0]["rationale"] == RATIONALE
    assert call("GET", f"/v1/events/{event_id}/report", "reviewer").content == report.content


def test_delta6_erasure_on_request_on_postgres(stack, clock):  # noqa: F811
    """Contract delta Δ6 / A4 on PostgreSQL: 409 before retain_until, 200 from it, then
    ALREADY_ERASED; one RECORD_ERASED entry by the reviewer; the chain still verifies. An event
    with no personal data gets an already-destroyed key (ck_record_key_destroyed_has_no_key)."""
    event_id = full_event(stack)
    with stack.make_uow() as uow:
        system_id = uow.events.get(uuid.UUID(event_id)).system_id
    later_sample = {
        "system_id": str(system_id),
        "metric_name": "accuracy",
        "metric_type": "performance",
        "value": 0.5,
        "timestamp": (T0 + timedelta(minutes=5)).isoformat(),
    }
    flagged = stack.call("POST", "/v1/metrics", "system", json=later_sample)
    assert flagged.status_code == 201, flagged.text
    bare_id = flagged.json()["flagged_event_id"]
    with stack.make_uow() as uow:
        retain_until = uow.events.get(uuid.UUID(event_id)).retain_until
    clock.advance(seconds=(retain_until - clock.now()).total_seconds() - 1)
    early = stack.call("POST", f"/v1/events/{event_id}/erasure", "reviewer")
    assert early.status_code == 409 and early.json()["error_code"] == "RETENTION_NOT_EXPIRED"

    clock.advance(seconds=1)
    resp = stack.call("POST", f"/v1/events/{event_id}/erasure", "reviewer", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["erased_fields"] == [
        "disposition.rationale",
        "harm_fields.evidence",
        "report.document",
    ]
    again = stack.call("POST", f"/v1/events/{event_id}/erasure", "reviewer")
    assert again.status_code == 409 and again.json()["error_code"] == "ALREADY_ERASED"
    assert stack.call("POST", f"/v1/events/{event_id}/erasure", "viewer").status_code == 403

    bare = stack.call("POST", f"/v1/events/{bare_id}/erasure", "reviewer")
    assert bare.status_code == 200 and bare.json()["erased_fields"] == []

    with stack.sessionmaker() as s:
        entries = s.scalars(
            select(LedgerEntry).where(LedgerEntry.entry_type == "RECORD_ERASED")
        ).all()
        assert [(str(e.event_id), e.payload["trigger"]) for e in entries] == [
            (event_id, "MANUAL"),
            (bare_id, "MANUAL"),
        ]
        assert {e.actor_id for e in entries} == {stack.keys["reviewer"].user_id}
        bare_key = s.get(RecordKey, uuid.UUID(bare_id))
        assert bare_key.wrapped_key is None and bare_key.destroyed_at is not None
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid
    # The retention job leaves both alone (already erased).
    clock.advance(days=2)
    assert erase_expired(stack.make_uow, ledger=get_ledger(), clock=clock).erased == []
