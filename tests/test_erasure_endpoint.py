"""POST /v1/events/{event_id}/erasure (contract delta Δ6, A4; REQ-16, REQ-05; D-29)."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.events.schemas import EventDetail
from app.modules.events.service import get_event
from app.modules.ledger.interface import LedgerEntryType, get_ledger
from app.modules.notifications.interface import get_notifier
from app.modules.records.service import erase_expired
from app.modules.review.disposition import FieldConfirmation
from app.modules.review.models import HarmFieldStatus
from tests.fakes import RecordingNotifier, system_principal, user_principal
from tests.test_records import ERASED_FIELDS, RATIONALE, full  # noqa: F401 - fixture
from tests.test_report import (  # noqa: F401 - fixtures
    EX1_FIELDS,
    NOT_SERIOUS,
    OTHER,
    SYSTEM,
    dispose,
    event_id,
    reviewer,
)


class Client:
    def __init__(self, uow, ledger, clock, principal) -> None:
        self.principal = principal
        app = create_app()
        app.dependency_overrides.update(
            {
                get_principal: lambda: self.principal,
                get_uow: lambda: uow,
                get_ledger: lambda: ledger,
                get_notifier: lambda: RecordingNotifier(),
                get_clock: lambda: clock,
            }
        )
        self.client = TestClient(app)

    def erase(self, event, **kwargs):
        return self.client.post(f"/v1/events/{event}/erasure", **kwargs)


@pytest.fixture
def api(uow, ledger, clock, reviewer):  # noqa: F811 - pytest fixture
    return Client(uow, ledger, clock, reviewer)


def to_retention_end(uow, clock, event):
    retain_until = uow.events.get(event).retain_until
    clock.advance(seconds=(retain_until - clock.now()).total_seconds())


def _view(uow, ledger, event):
    record = get_event(event, principal=user_principal(Role.VIEWER), uow=uow, ledger=ledger)
    return EventDetail.from_record(record)


class TestErase:
    def test_at_retain_until_a_reviewer_erases(self, api, uow, ledger, clock, full, reviewer):  # noqa: F811
        to_retention_end(uow, clock, full)
        ledger.calls.clear()
        resp = api.erase(full)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "event_id": str(full),
            "erased_at": clock.now().isoformat().replace("+00:00", "Z"),
            "erased_fields": ERASED_FIELDS,
        }
        (entry,) = ledger.calls
        assert entry["entry_type"] == LedgerEntryType.RECORD_ERASED
        assert entry["actor_id"] == reviewer.actor_id
        assert entry["payload"]["trigger"] == "MANUAL"
        assert entry["payload"]["erased_fields"] == ERASED_FIELDS
        detail = _view(uow, ledger, full)
        assert detail.personal_data_erased_at == clock.now()
        assert [r.rationale for r in detail.revision_history] == [None]

    @pytest.mark.parametrize("body", [None, {}])
    def test_empty_body_or_empty_object(self, api, uow, clock, full, body):  # noqa: F811
        to_retention_end(uow, clock, full)
        kwargs = {} if body is None else {"json": body}
        assert api.erase(full, **kwargs).status_code == 200

    def test_before_retain_until_is_retention_not_expired(self, api, uow, ledger, clock, full):  # noqa: F811
        to_retention_end(uow, clock, full)
        clock.advance(seconds=-1)
        ledger.calls.clear()
        resp = api.erase(full)
        assert resp.status_code == 409
        body = resp.json()
        assert body["error_code"] == "RETENTION_NOT_EXPIRED"
        assert "retained until" in body["message"] and body["detail"] == body["message"]
        assert ledger.calls == []
        assert uow.record_keys.get(full).wrapped_key is not None

    def test_twice_is_already_erased(self, api, uow, ledger, clock, full):  # noqa: F811
        to_retention_end(uow, clock, full)
        assert api.erase(full).status_code == 200
        calls = len(ledger.calls)
        resp = api.erase(full)
        assert resp.status_code == 409
        assert resp.json()["error_code"] == "ALREADY_ERASED"
        assert len(ledger.calls) == calls

    def test_after_the_retention_job_is_already_erased(self, api, uow, ledger, clock, full):  # noqa: F811
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        assert api.erase(full).json()["error_code"] == "ALREADY_ERASED"

    def test_the_retention_job_skips_an_erased_event(self, api, uow, ledger, clock, full):  # noqa: F811
        to_retention_end(uow, clock, full)
        api.erase(full)
        clock.advance(days=400)
        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == []

    def test_an_event_without_personal_data(self, api, uow, ledger, clock, event_id, reviewer):  # noqa: F811
        """No key yet: erasing records a destroyed one, so later dispositions are refused."""
        to_retention_end(uow, clock, event_id)
        resp = api.erase(event_id)
        assert resp.status_code == 200 and resp.json()["erased_fields"] == []
        key = uow.record_keys.get(event_id)
        assert key.wrapped_key is None and key.destroyed_at == clock.now()
        resp = api.client.post(f"/v1/events/{event_id}/disposition", json={"tier": "DEATH"})
        assert resp.status_code == 409 and resp.json()["error_code"] == "ALREADY_ERASED"

    def test_only_the_fields_that_held_data(self, api, uow, ledger, clock, event_id, reviewer):  # noqa: F811
        all_na = {name: FieldConfirmation(HarmFieldStatus.NOT_APPLICABLE) for name in EX1_FIELDS}
        dispose(
            uow, ledger, clock, event_id, reviewer, NOT_SERIOUS, rationale=RATIONALE, fields=all_na
        )
        to_retention_end(uow, clock, event_id)
        assert api.erase(event_id).json()["erased_fields"] == ["disposition.rationale"]


class TestRefused:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.ADMIN])
    def test_non_reviewers_are_403(self, api, uow, ledger, clock, full, role):  # noqa: F811
        to_retention_end(uow, clock, full)
        api.principal = user_principal(role)
        assert api.erase(full).status_code == 403
        assert uow.record_keys.get(full).wrapped_key is not None

    def test_system_keys_are_403(self, api, full):  # noqa: F811
        api.principal = system_principal(SYSTEM)
        assert api.erase(full).status_code == 403

    @pytest.mark.parametrize("event", ["00000000-0000-4000-8000-000000000000", "not-a-uuid"])
    def test_unknown_or_malformed_id_is_404(self, api, event):
        assert api.erase(event).status_code == 404

    @pytest.mark.parametrize("body", [{"now": True}, [], "x"])
    def test_any_other_body_is_malformed(self, api, uow, clock, full, body):  # noqa: F811
        to_retention_end(uow, clock, full)
        resp = api.erase(full, json=body)
        assert resp.status_code == 400 and resp.json()["error_code"] == "MALFORMED_BODY"
        assert uow.record_keys.get(full).wrapped_key is not None


def test_without_a_key_it_is_401():
    """The delta's acceptance check: each new route answers 401 without a key."""
    resp = TestClient(create_app()).post(f"/v1/events/{SYSTEM}/erasure")
    assert resp.status_code == 401


def test_documented_in_openapi():
    op = create_app().openapi()["paths"]["/v1/events/{event_id}/erasure"]["post"]
    assert {"200", "400", "401", "403", "404", "409"} <= set(op["responses"])


def test_window_is_retain_until_not_the_retention_job_cutoff(api, uow, clock, full):  # noqa: F811
    """On request, erasure is allowed from retain_until; the worker waits a day longer (D-28)."""
    to_retention_end(uow, clock, full)
    assert api.erase(full).status_code == 200
    assert clock.now() < uow.events.get(full).retain_until + timedelta(days=1)


_ = (OTHER,)
