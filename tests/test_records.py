"""Erasable personal data with per-event keys (REQ-16, AC-18; REQ-05; D-28)."""

import base64
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.core.config import AppEnv, Settings, get_settings
from app.core.errors import UnimplementedError
from app.modules.auth.interface import Role
from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.events.schemas import EventDetail
from app.modules.events.service import get_event
from app.modules.ledger.interface import LedgerEntryType
from app.modules.records import crypto
from app.modules.records.service import (
    erase_expired,
    erasure_due_at,
    evidence_purpose,
    master_key,
    note_purpose,
    rationale_purpose,
    report_purpose,
)
from app.modules.review.disposition import DispositionConflict
from app.modules.review.report import ReportUnavailable
from tests.fakes import ChainLedger, OutboxRecorder, RecordingSender, opened, user_principal
from tests.test_report import (  # noqa: F401 - fixtures
    EX1_FIELDS,
    NOT_SERIOUS,
    OTHER,
    Http,
    dispose,
    event_id,
    report,
    reviewer,
    submit,
)

RATIONALE = "reviewer spoke to the affected applicant, Ms Example"
EVIDENCE = "credit denied to applicants"
NOTE = "sent to the authority by J. Doe"
SECRETS = (RATIONALE, EVIDENCE, NOTE)


@pytest.fixture(autouse=True)
def _fresh_key_settings():
    get_settings.cache_clear()
    master_key.cache_clear()
    yield
    get_settings.cache_clear()
    master_key.cache_clear()


# --- crypto --------------------------------------------------------------------------------


class TestCrypto:
    def test_round_trip_and_token_form(self):
        key, event = crypto.new_data_key(), uuid4()
        token = crypto.seal(key, event, "p", "naïve text ✓")
        assert token.startswith("enc:v1:") and "naïve" not in token
        assert crypto.open_token(key, event, "p", token) == "naïve text ✓"

    def test_each_seal_uses_a_fresh_nonce(self):
        key, event = crypto.new_data_key(), uuid4()
        assert crypto.seal(key, event, "p", "same") != crypto.seal(key, event, "p", "same")

    @pytest.mark.parametrize("change", ["event", "purpose", "key", "bytes"])
    def test_a_token_opens_only_for_its_event_purpose_and_key(self, change):
        key, event = crypto.new_data_key(), uuid4()
        token = crypto.seal(key, event, "disposition.rationale:1", "secret")
        args = {"key": key, "event": event, "purpose": "disposition.rationale:1", "token": token}
        if change == "event":
            args["event"] = uuid4()
        elif change == "purpose":
            args["purpose"] = "disposition.rationale:2"
        elif change == "key":
            args["key"] = crypto.new_data_key()
        else:
            raw = bytearray(base64.urlsafe_b64decode(token[len("enc:v1:") :]))
            raw[-1] ^= 1
            args["token"] = "enc:v1:" + base64.urlsafe_b64encode(bytes(raw)).decode()
        with pytest.raises(crypto.RecordKeyError):
            crypto.open_token(args["key"], args["event"], args["purpose"], args["token"])

    def test_plain_text_is_not_a_token(self):
        with pytest.raises(crypto.RecordKeyError):
            crypto.open_token(crypto.new_data_key(), uuid4(), "p", "plain")

    def test_wrapped_key_needs_the_master_key_and_its_event(self):
        master, event, data_key = crypto.new_data_key(), uuid4(), crypto.new_data_key()
        wrapped = crypto.wrap_key(master, event, data_key)
        assert data_key not in wrapped
        assert crypto.unwrap_key(master, event, wrapped) == data_key
        with pytest.raises(crypto.RecordKeyError):
            crypto.unwrap_key(crypto.new_data_key(), event, wrapped)
        with pytest.raises(crypto.RecordKeyError):
            crypto.unwrap_key(master, uuid4(), wrapped)

    def test_master_key_format(self):
        raw = bytes(range(32))
        assert crypto.parse_master_key(base64.b64encode(raw).decode()) == raw
        assert crypto.parse_master_key(base64.urlsafe_b64encode(raw).decode()) == raw
        for bad in ("not base64!", base64.b64encode(b"short").decode()):
            with pytest.raises(ValueError):
                crypto.parse_master_key(bad)


class TestMasterKey:
    def test_configured_key_is_used(self, monkeypatch):
        raw = bytes(range(32))
        monkeypatch.setenv("RECORD_KEY_MASTER_KEY", base64.b64encode(raw).decode())
        assert master_key() == raw

    def test_development_key_when_unset_outside_production(self, monkeypatch, caplog):
        monkeypatch.delenv("RECORD_KEY_MASTER_KEY", raising=False)
        assert master_key() == crypto.DEVELOPMENT_MASTER_KEY
        assert "protects nothing" in caplog.text

    def test_production_refuses_to_seal_without_a_key(self, monkeypatch):
        monkeypatch.delenv("RECORD_KEY_MASTER_KEY", raising=False)
        monkeypatch.setenv("APP_ENV", "production")
        with pytest.raises(RuntimeError, match="RECORD_KEY_MASTER_KEY is required"):
            master_key()

    def test_malformed_key_is_rejected_at_startup(self):
        with pytest.raises(ValidationError, match="RECORD_KEY_MASTER_KEY"):
            Settings(record_key_master_key="too-short", app_env=AppEnv.TEST)


# --- when erasure is due -------------------------------------------------------------------


def _event(detected_at: datetime, retain_until: datetime | None = None) -> Event:
    return Event(
        id=uuid4(),
        type=EventType.THRESHOLD_BREACH,
        state=EventState.FLAGGED,
        detected_at=detected_at,
        retain_until=retain_until or retain_until_for(detected_at),
    )


class TestErasureDue:
    def test_a_day_after_the_six_month_minimum(self):
        # D-18's example: retain_until is 2027-03-27 20:00 but AC-05's literal 5mo29d is
        # 2027-03-28 20:00, so erasure waits until then; the record itself is never deleted.
        event = _event(datetime(2026, 9, 27, 20, 0, tzinfo=UTC))
        assert event.retain_until == datetime(2027, 3, 27, 20, 0, tzinfo=UTC)
        assert erasure_due_at(event) == datetime(2027, 3, 28, 20, 0, tzinfo=UTC)

    def test_a_longer_retention_wins(self):
        detected = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)
        event = _event(detected, retain_until=datetime(2028, 1, 1, tzinfo=UTC))
        assert erasure_due_at(event) == datetime(2028, 1, 1, tzinfo=UTC)


# --- the whole lifecycle on the doubles (AC-18) ----------------------------------------------


@pytest.fixture
def full(uow, ledger, clock, event_id, reviewer):  # noqa: F811 - pytest fixtures
    """Ex-1 with every kind of personal data: evidence, rationale, report and note."""
    dispose(uow, ledger, clock, event_id, reviewer, OTHER, rationale=RATIONALE, fields=EX1_FIELDS)
    report(uow, ledger, clock, event_id, reviewer)
    submit(uow, ledger, clock, event_id, reviewer, note=NOTE)
    return event_id


def _stored(uow, event):
    (d,) = uow.dispositions.for_event(event)
    rights = next(f for f in uow.harm_fields.for_event(event) if f.name == "fundamental_rights")
    row = uow.reports.get(event, 1)
    return {
        rationale_purpose(1): d.rationale,
        evidence_purpose("fundamental_rights"): rights.evidence,
        report_purpose(1): row.document,
        note_purpose(1): row.submission_note,
    }


def _view(uow, ledger, event):
    return EventDetail.from_record(
        get_event(event, principal=user_principal(Role.VIEWER), uow=uow, ledger=ledger)
    )


class TestLifecycle:
    def test_personal_data_is_never_stored_or_logged_in_clear(self, uow, ledger, full):
        stored = _stored(uow, full)
        for purpose, value in stored.items():
            assert crypto.is_token(value), purpose
        assert opened(uow, full, rationale_purpose(1), stored[rationale_purpose(1)]) == RATIONALE
        assert opened(uow, full, note_purpose(1), stored[note_purpose(1)]) == NOTE
        assert RATIONALE in opened(uow, full, report_purpose(1), stored[report_purpose(1)])
        logged = repr(ledger.calls)
        for secret in SECRETS:
            assert secret not in logged
        assert stored[rationale_purpose(1)] in logged  # the ledger holds the same tokens

    def test_readable_until_erasure_is_due(self, uow, ledger, clock, full):
        due = erasure_due_at(uow.events.get(full))
        clock.advance(seconds=(due - clock.now()).total_seconds() - 1)
        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == []
        detail = _view(uow, ledger, full)
        assert detail.revision_history[0].rationale == RATIONALE
        assert detail.personal_data_erased_at is None

    def test_erased_once_due_with_one_ledger_entry(self, uow, ledger, clock, full):
        before = _stored(uow, full)
        event = uow.events.get(full)
        due = erasure_due_at(event)
        clock.advance(seconds=(due - clock.now()).total_seconds())
        ledger.calls.clear()

        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == [full]
        key = uow.record_keys.get(full)
        assert (key.wrapped_key, key.destroyed_at) == (None, due)
        assert ledger.calls == [
            {
                "session": None,
                "entry_type": LedgerEntryType.PERSONAL_DATA_ERASED,
                "actor_id": None,
                "payload": {
                    "event_id": str(full),
                    "retain_until": event.retain_until.isoformat(),
                    "erased_at": due.isoformat(),
                },
                "event_id": full,
            }
        ]
        # Nothing stored was rewritten (so no hash changes); it just can't be opened any more.
        assert _stored(uow, full) == before
        for purpose, value in before.items():
            assert opened(uow, full, purpose, value) is None

    def test_after_erasure_the_event_reads_without_personal_data(
        self,
        uow,
        ledger,
        clock,
        full,
        reviewer,  # noqa: F811 - pytest fixture
    ):
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        detail = _view(uow, ledger, full)
        assert detail.personal_data_erased_at == clock.now()
        assert [r.rationale for r in detail.revision_history] == [None]
        # Everything else about the event stays (REQ-05: the record itself is retained).
        assert detail.state == EventState.DISPOSITIONED
        assert detail.harm_fields["fundamental_rights"].status == "CONFIRMED"
        assert detail.observed_value is not None and detail.deadline_utc is not None

    def test_erased_events_refuse_reports_and_new_dispositions(
        self,
        uow,
        ledger,
        clock,
        full,
        reviewer,  # noqa: F811 - pytest fixture
    ):
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)
        ledger.calls.clear()
        with pytest.raises(ReportUnavailable, match="erased"):
            report(uow, ledger, clock, full, reviewer)
        with pytest.raises(DispositionConflict, match="erased"):
            dispose(uow, ledger, clock, full, reviewer, NOT_SERIOUS, rationale="late")
        assert ledger.calls == [] and len(uow.dispositions.for_event(full)) == 1

    def test_erased_only_once(self, uow, ledger, clock, full):
        clock.advance(days=400)
        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == [full]
        calls = len(ledger.calls)
        clock.advance(days=1)
        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == []
        assert len(ledger.calls) == calls

    def test_ledger_failure_keeps_the_key(self, uow, ledger, clock, full):
        clock.advance(days=400)
        ledger.fail_with = UnimplementedError("x", "REQ-06", "ledger")
        with pytest.raises(UnimplementedError):
            erase_expired(lambda: uow, ledger=ledger, clock=clock)
        assert uow.record_keys.get(full).wrapped_key is not None
        ledger.fail_with = None
        assert opened(uow, full, note_purpose(1), uow.reports.get(full, 1).submission_note) == NOTE

    def test_events_without_personal_data_have_no_key(self, uow, ledger, clock, event_id):  # noqa: F811
        assert uow.record_keys.get(event_id) is None
        clock.advance(days=400)
        assert erase_expired(lambda: uow, ledger=ledger, clock=clock).erased == []
        assert _view(uow, ledger, event_id).personal_data_erased_at is None


class TestHttp:
    def test_event_and_report_after_erasure(self, uow, ledger, clock, full, reviewer):  # noqa: F811
        http = Http(uow, ledger, clock, reviewer)
        assert http.get(full).status_code == 200
        clock.advance(days=400)
        erase_expired(lambda: uow, ledger=ledger, clock=clock)

        body = http.client.get(f"/v1/events/{full}").json()
        assert body["personal_data_erased_at"] is not None
        assert body["revision_history"][0]["rationale"] is None
        resp = http.get(full)
        assert resp.status_code == 409
        assert "erased" in resp.json()["detail"]
        for secret in SECRETS:
            assert secret not in resp.text


def test_the_worker_erases_due_events(uow, clock, ledger, full):
    from app.modules.worker.runner import Worker

    chain = ChainLedger(uow.ledger, clock)
    worker = Worker(
        uow_factory=lambda: uow,
        ledger=chain,
        notifier=OutboxRecorder(uow.notifications, clock),
        sender=RecordingSender(),
        clock_provider=lambda: clock,
        max_attempts=3,
    )
    assert worker.run_once().erasure.erased == []
    clock.advance(days=400)
    result = worker.run_once()
    assert result.erasure.erased == [full] and "erasure" not in result.errors
    erased = [e for e in uow.ledger.entries if e.entry_type == LedgerEntryType.PERSONAL_DATA_ERASED]
    assert [e.event_id for e in erased] == [full]
