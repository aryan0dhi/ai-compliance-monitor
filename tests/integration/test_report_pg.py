"""GET /v1/events/{id}/report and POST /v1/events/{id}/report/submission on PostgreSQL with the
production auth and ledger (REQ-12, REQ-13, REQ-14; B-12; AC-13; D-25)."""

import hashlib
import json
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
from app.modules.records.service import report_purpose
from app.modules.review.models import HarmFieldName, Report, ReportSubmission
from app.modules.review.report import get_report
from tests.conftest import T0
from tests.fakes import FixedClock, opened, user_principal
from tests.test_report_schema import violations

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
        "admin": create_user(make_uow(), name="admin", role=Role.ADMIN),
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
            resp = self.call("POST", f"/v1/events/{event_id}/disposition", role, json=body)
            assert resp.status_code == 200, resp.text
            return resp

        def report(self, event_id, role="reviewer"):
            return self.call("GET", f"/v1/events/{event_id}/report", role)

        def submit(self, event_id, role="reviewer", **kwargs):
            return self.call("POST", f"/v1/events/{event_id}/report/submission", role, **kwargs)

        def audit(self, event_id):
            return self.call("GET", f"/v1/events/{event_id}/audit", "viewer").json()

    s = Stack()
    s.keys, s.make_uow, s.sessionmaker = keys, make_uow, pg_sessionmaker
    return s


def test_model5_ex1_report_after_revision(stack, clock):
    """Ex-1: OTHER_SERIOUS at d0 13:00, DEATH at d3, report at d10 13:00 (AC-13)."""
    event_id = stack.breach()
    clock.advance(hours=4)
    stack.dispose(event_id, {"tier": "OTHER_SERIOUS", "harm_fields": EX1_FIELDS})
    clock.advance(days=3)
    stack.dispose(event_id, {"tier": "DEATH"}, role="reviewer2")
    clock.advance(days=7)

    first = stack.report(event_id)
    assert first.status_code == 200, first.text
    body = first.json()
    assert violations(body) == []  # Δ1 report.schema.json (D-30)
    assert body["timestamps"] == {
        "detected_at": "2026-09-27T20:01:00Z",
        "determined_at": "2026-09-28T00:01:00Z",  # A, the first serious determination
        "last_revised_at": "2026-10-01T00:01:00Z",
        "assembled_at": "2026-10-08T00:01:00Z",
    }
    determination = body["determination"]
    assert determination["deadline_utc"] == "2026-10-08T00:01:00Z"  # A + 10 days
    assert (determination["tier"], determination["reviewer_id"], determination["rationale"]) == (
        "DEATH",
        str(stack.keys["reviewer2"].user_id),
        None,
    )
    assert [(r["prior_tier"], r["new_tier"]) for r in determination["revisions"]] == [
        ("OTHER_SERIOUS", "DEATH")
    ]
    assert body["risk_indicator"] == {"detection_to_assignment_seconds": 4 * 3600}
    assert body["harm_fields"]["fundamental_rights_infringement"]["status"] == "CONFIRMED"
    assert body["event"]["observed_value"] == 0.61
    assert [s["value"] for s in body["event"]["evidence_bundle"]["samples"]] == [0.61]
    # The anchor is a real ledger entry from before the report was assembled.
    anchor = body["ledger_anchor"]
    with stack.make_uow() as uow:
        entries = uow.ledger.page(after_seq=0, limit=1000)
    (anchored,) = [e for e in entries if e.seq == anchor["seq"]]
    assert anchored.entry_hash == anchor["hash"]
    assert all(e.entry_type != "REPORT_ASSEMBLED" for e in entries if e.seq <= anchor["seq"])

    # B-12: byte-for-byte the same on later calls, by another reviewer, a day later.
    clock.advance(days=1)
    second = stack.report(event_id, role="reviewer2")
    assert second.status_code == 200
    assert second.content == first.content

    with stack.sessionmaker() as s:
        stored = s.scalars(select(Report)).one()
        assert stored.disposition_seq == 2
        sealed, sha = stored.document, stored.report_sha256
    with stack.make_uow() as uow:
        # Stored sealed (REQ-16); the hash is over the document as assembled and served.
        document = opened(uow, event_id, report_purpose(2), sealed)
        assert sha == hashlib.sha256(document.encode()).hexdigest()
        assert first.content.decode() == document  # served as stored (D-30)

    actions = [e["action"] for e in stack.audit(event_id)]
    assert actions[-2:] == ["REPORT_ASSEMBLED", "VIEW"]
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid


def submission_body(stack, event_id, **extra):
    """Contract delta Δ3: submit the event's current report version, by id and hash."""
    (version,) = [
        v
        for v in stack.call("GET", f"/v1/events/{event_id}/reports", "viewer").json()
        if v["status"] in ("ASSEMBLED", "SUBMITTED")
    ]
    return {
        "report_id": version["report_id"],
        "json_sha256": version["report_sha256"],
        "report_type": "Initial",
        **extra,
    }


DUMMY_SUBMISSION = {
    "report_id": str(uuid.UUID(int=1)),
    "json_sha256": "0" * 64,
    "report_type": "Initial",
}


def test_submission(stack, clock):
    event_id = stack.breach()
    stack.dispose(event_id, {"tier": "OTHER_SERIOUS", "harm_fields": EX1_FIELDS})
    assert stack.call("GET", f"/v1/events/{event_id}/reports", "viewer").json() == []
    assembled = stack.report(event_id)
    body = submission_body(stack, event_id, authority_reference="MSA-2026-0042")
    clock.advance(hours=2)

    resp = stack.submit(event_id, json=body)
    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert (out["submitted_at"], out["submission_kind"]) == ("2026-09-27T22:01:00Z", "INITIAL")
    assert out["authority_reference"] == "MSA-2026-0042"
    # The report itself doesn't change (D-30); the submission shows in the version list.
    assert stack.report(event_id).content == assembled.content
    (listed,) = stack.call("GET", f"/v1/events/{event_id}/reports", "viewer").json()
    assert (listed["status"], listed["submitted_at"]) == ("SUBMITTED", "2026-09-27T22:01:00Z")
    again = stack.submit(event_id, json=body)
    assert again.status_code == 409 and again.json()["error_code"] == "REPORT_ALREADY_SUBMITTED"
    by_id = stack.call("GET", f"/v1/reports/{body['report_id']}", "reviewer")
    assert by_id.content == stack.report(event_id).content
    versions = stack.call("GET", f"/v1/events/{event_id}/reports", "viewer").json()
    assert [v["status"] for v in versions] == ["SUBMITTED"]

    with stack.sessionmaker() as s:
        stored = s.scalars(select(Report)).one()
        assert stored.submitted_by == stack.keys["reviewer"].user_id
        submission = s.scalars(select(ReportSubmission)).one()
        assert str(submission.id) == out["submission_id"] and submission.report_id == stored.id
        entry = s.scalars(
            select(LedgerEntry).where(LedgerEntry.entry_type == "REPORT_SUBMITTED")
        ).one()
        assert entry.payload["submission_id"] == out["submission_id"]
        assert entry.payload["report_sha256"] == stored.report_sha256
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid


def test_refusals_change_nothing_and_are_audited(stack):
    """REQ-08a, REQ-13: 409 before a serious tier or after NOT_SERIOUS; 403 for other roles.
    Each refusal on a real event adds one REFUSED entry (D-26); an unknown id adds none."""
    event_id = stack.breach()
    before = stack.audit(event_id)
    assert stack.report(event_id).status_code == 409
    for role in ("viewer", "admin", "system"):
        assert stack.report(event_id, role=role).status_code == 403
        assert stack.submit(event_id, role=role, json=DUMMY_SUBMISSION).status_code == 403
    assert stack.report(str(uuid.uuid4())).status_code == 404
    new = stack.audit(event_id)[len(before) :]
    assert [e["action"] for e in new] == ["REFUSED"] * 7

    stack.dispose(event_id, {"tier": "NOT_SERIOUS", "rationale": "no harm", "harm_fields": ALL_NA})
    after = stack.audit(event_id)
    assert stack.report(event_id).status_code == 409
    assert [e["action"] for e in stack.audit(event_id)[len(after) :]] == ["REFUSED"]
    with stack.sessionmaker() as s:
        assert s.scalars(select(Report)).all() == []


def test_concurrent_first_requests_assemble_once(stack):
    """The event row lock serializes assembly: one report and one REPORT_ASSEMBLED entry."""
    event_id = stack.breach()
    stack.dispose(event_id, {"tier": "DEATH", "harm_fields": EX1_FIELDS})
    reviewer = user_principal(Role.REVIEWER).model_copy(
        update={"actor_id": stack.keys["reviewer"].user_id}
    )
    from app.core.clock import get_clock
    from app.modules.ledger.interface import get_ledger

    barrier, errors, documents = threading.Barrier(6), [], []

    def fetch():
        barrier.wait()
        try:
            report = get_report(
                uuid.UUID(event_id),
                principal=reviewer,
                uow=stack.make_uow(),
                ledger=get_ledger(),
                clock=get_clock(),
            )
            documents.append(report.document)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(set(documents)) == 1 and len(documents) == 6
    with stack.sessionmaker() as s:
        assert len(s.scalars(select(Report)).all()) == 1
        types = s.scalars(
            select(LedgerEntry.entry_type).where(LedgerEntry.entry_type.like("REPORT_%"))
        ).all()
        assert types == ["REPORT_ASSEMBLED"]
    assert json.loads(documents[0])["determination"]["tier"] == "DEATH"
    assert verify_ledger(principal=user_principal(Role.VIEWER), uow=stack.make_uow()).valid
