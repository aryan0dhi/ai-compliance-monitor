"""Audit ledger: hash chain (REQ-06, AC-06, Ex-5), audit trail (REQ-14, AC-15), export/verify."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.chain import (
    GENESIS_HASH,
    compute_entry_hash,
    format_timestamp,
    require_json_safe,
    verify_chain,
)
from app.modules.ledger.interface import LedgerEntryType
from app.modules.ledger.service import (
    AuditEventNotFound,
    LedgerAccessDenied,
    event_audit,
    export_ledger,
    verify_ledger,
)
from tests.conftest import T0
from tests.fakes import ChainLedger, FixedClock, system_principal, user_principal
from tests.test_event_queries import make_event

ROOT = Path(__file__).resolve().parents[1]
VIEWER = user_principal(Role.VIEWER)


def load_offline_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_ledger_export", ROOT / "scripts" / "verify_ledger_export.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def chain(uow, clock):
    return ChainLedger(uow.ledger, clock)


@pytest.fixture
def filled(uow, chain, clock):
    """Five entries for one event: FLAGGED then four views by different actors."""
    system_id = uow.seed_system()
    event = make_event(system_id)
    uow.events.rows[event.id] = event
    chain.append(
        None, entry_type="FLAGGED", actor_id=None, payload={"v": "0.35"}, event_id=event.id
    )
    actors = [uuid4() for _ in range(4)]
    for actor in actors:
        clock.advance(minutes=1)
        chain.append(None, entry_type="VIEW", actor_id=actor, payload={}, event_id=event.id)
    return event, actors


class TestChain:
    def test_entries_link_from_genesis(self, uow, filled):
        entries = uow.ledger.entries
        assert entries[0].prev_hash == GENESIS_HASH
        assert [e.seq for e in entries] == [1, 2, 3, 4, 5]
        for prev, entry in zip(entries, entries[1:], strict=False):
            assert entry.prev_hash == prev.entry_hash  # INV-7

    def test_intact_chain_verifies(self, uow, filled):
        result = verify_chain(uow.ledger.entries)
        assert (result.valid, result.entries_checked, result.first_mismatch) == (True, 5, None)
        assert (result.head_seq, result.head_hash) == (5, uow.ledger.entries[-1].entry_hash)

    def test_empty_chain_is_valid(self):
        result = verify_chain([])
        assert (result.valid, result.entries_checked, result.head_seq) == (True, 0, 0)

    # AC-06 / Ex-5: modification bypassing the application is detected on the next pass.
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("payload", {"v": "0.99"}),
            ("actor_id", UUID(int=7)),
            ("entry_type", "FLAGGED"),
            ("at", T0 - timedelta(days=1)),
            ("event_id", None),
        ],
    )
    def test_ac06_edited_entry_is_detected(self, uow, filled, field, value):
        setattr(uow.ledger.entries[2], field, value)  # seq 3
        result = verify_chain(uow.ledger.entries)
        assert (result.valid, result.first_mismatch) == (False, 3)
        assert result.head_seq == 2  # last entry that still verified

    def test_edit_with_recomputed_hash_breaks_the_next_link(self, uow, filled):
        # A tamperer who also fixes the edited entry's own hash is caught by its successor.
        e = uow.ledger.entries[2]
        e.payload = {"forged": True}
        e.entry_hash = compute_entry_hash(
            seq=e.seq,
            entry_type=e.entry_type,
            event_id=e.event_id,
            actor_id=e.actor_id,
            at=e.at,
            payload=e.payload,
            prev_hash=e.prev_hash,
        )
        assert verify_chain(uow.ledger.entries).first_mismatch == 4

    def test_deleted_entry_is_detected(self, uow, filled):
        del uow.ledger.entries[1]
        assert verify_chain(uow.ledger.entries).first_mismatch == 3

    def test_reordered_entries_are_detected(self, uow, filled):
        entries = uow.ledger.entries
        entries[1], entries[2] = entries[2], entries[1]
        assert verify_chain(entries).first_mismatch == 3

    def test_hash_covers_every_field(self):
        base = dict(
            seq=1,
            entry_type="VIEW",
            event_id=None,
            actor_id=None,
            at=T0,
            payload={},
            prev_hash=GENESIS_HASH,
        )
        variants = [
            {"seq": 2},
            {"entry_type": "FLAGGED"},
            {"event_id": UUID(int=1)},
            {"actor_id": UUID(int=1)},
            {"at": T0 + timedelta(microseconds=1)},
            {"payload": {"a": 1}},
            {"prev_hash": "1" * 64},
        ]
        hashes = {compute_entry_hash(**{**base, **v}) for v in variants}
        assert len(hashes) == len(variants) and compute_entry_hash(**base) not in hashes

    def test_timestamps_are_canonical_utc(self):
        from zoneinfo import ZoneInfo

        local = T0.astimezone(ZoneInfo("America/New_York"))
        assert format_timestamp(local) == format_timestamp(T0) == "2026-09-27T20:00:00.000000+00:00"
        with pytest.raises(ValueError):
            format_timestamp(datetime(2026, 1, 1))

    @pytest.mark.parametrize(
        "payload",
        [{"x": 0.5}, {"x": [1, 2.0]}, {"x": {"y": float("nan")}}, {1: "a"}, {"x": object()}],
    )
    def test_unstable_payload_values_are_rejected(self, payload):
        with pytest.raises(TypeError):
            require_json_safe(payload)

    def test_unknown_entry_type_rejected(self, chain):
        with pytest.raises(ValueError):
            chain.append(None, entry_type="EDIT", actor_id=None, payload={})


class TestOfflineVerifier:
    """scripts/verify_ledger_export.py must agree with the service, byte for byte."""

    def export_json(self, uow) -> list[dict]:
        from app.modules.ledger.schemas import LedgerEntryOut

        return [
            json.loads(LedgerEntryOut.from_entry(e).model_dump_json()) for e in uow.ledger.entries
        ]

    def test_agrees_on_an_intact_export(self, uow, filled):
        offline = load_offline_verifier().verify(self.export_json(uow))
        online = verify_chain(uow.ledger.entries)
        assert offline == {
            "valid": True,
            "entries_checked": 5,
            "first_mismatch": None,
            "head_seq": 5,
            "head_hash": online.head_hash,
        }

    def test_detects_a_tampered_export(self, uow, filled, tmp_path):
        entries = self.export_json(uow)
        entries[3]["payload"] = {"tampered": True}
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({"entries": entries}))
        verifier = load_offline_verifier()
        assert verifier.main([str(path)]) == 1
        assert verifier.verify(entries)["first_mismatch"] == 4

    def test_detects_truncation_against_a_recorded_head(self, uow, filled, tmp_path):
        entries = self.export_json(uow)
        head = entries[-1]
        path = tmp_path / "ledger.json"
        path.write_text(json.dumps({"entries": entries[:-1]}))  # last entry removed
        verifier = load_offline_verifier()
        assert verifier.main([str(path)]) == 0  # the chain alone can't show it
        args = [str(path), "--expect-head-seq", "5", "--expect-head-hash", head["entry_hash"]]
        assert verifier.main(args) == 1


class TestServices:
    def test_export_pages(self, uow, filled):
        first = export_ledger(after_seq=0, limit=2, principal=VIEWER, uow=uow)
        assert [e.seq for e in first.entries] == [1, 2] and first.next_after_seq == 2
        last = export_ledger(after_seq=4, limit=2, principal=VIEWER, uow=uow)
        assert [e.seq for e in last.entries] == [5] and last.next_after_seq is None

    def test_verify_walks_every_page(self, uow, filled, monkeypatch):
        from app.modules.ledger import service

        monkeypatch.setattr(service, "VERIFY_BATCH_SIZE", 2)
        assert verify_ledger(principal=VIEWER, uow=uow).entries_checked == 5

    def test_verify_failure_is_logged_critical(self, uow, filled, caplog):
        uow.ledger.entries[0].payload = {"v": "tampered"}
        result = verify_ledger(principal=VIEWER, uow=uow)
        assert not result.valid
        assert any(r.levelname == "CRITICAL" for r in caplog.records)

    def test_ac15_audit_lists_actor_and_time_of_every_access(self, uow, filled):
        event, actors = filled
        entries = event_audit(event.id, principal=VIEWER, uow=uow)
        assert [e.entry_type for e in entries] == ["FLAGGED"] + ["VIEW"] * 4
        assert [e.actor_id for e in entries[1:]] == actors
        assert [e.at for e in entries] == sorted(e.at for e in entries)

    def test_audit_unknown_event(self, uow):
        with pytest.raises(AuditEventNotFound):
            event_audit(uuid4(), principal=VIEWER, uow=uow)

    @pytest.mark.parametrize(
        "call",
        [
            lambda p, uow: export_ledger(after_seq=0, limit=1, principal=p, uow=uow),
            lambda p, uow: verify_ledger(principal=p, uow=uow),
            lambda p, uow: event_audit(uuid4(), principal=p, uow=uow),
        ],
    )
    def test_system_keys_cannot_read_the_ledger(self, uow, call):
        with pytest.raises(LedgerAccessDenied):
            call(system_principal(uuid4()), uow)


class TestRoutes:
    @pytest.fixture
    def client(self, uow, filled):
        app = create_app()
        app.dependency_overrides.update({get_principal: lambda: VIEWER, get_uow: lambda: uow})
        return TestClient(app)

    def test_verify(self, client, uow):
        body = client.get("/v1/ledger/verify").json()
        assert body == {
            "valid": True,
            "entries_checked": 5,
            "first_mismatch": None,
            "head_seq": 5,
            "head_hash": uow.ledger.entries[-1].entry_hash,
        }

    def test_verify_reports_tamper(self, client, uow):
        uow.ledger.entries[1].payload = {"edited": True}
        body = client.get("/v1/ledger/verify").json()
        assert body["valid"] is False and body["first_mismatch"] == 2

    def test_export_round_trips_through_the_offline_verifier(self, client):
        body = client.get("/v1/ledger/export?limit=3").json()
        assert body["next_after_seq"] == 3 and "sha256" in body["hash_algorithm"]
        rest = client.get("/v1/ledger/export?after_seq=3").json()
        assert rest["next_after_seq"] is None
        entries = body["entries"] + rest["entries"]
        assert load_offline_verifier().verify(entries)["valid"] is True
        assert entries[0]["at"].endswith("+00:00")

    @pytest.mark.parametrize(
        "query",
        ["limit=0", "limit=10001", "after_seq=-1", "after_seq=x", "after_seq=0.0", "limit=5.0"],
    )
    def test_export_invalid_query_is_400(self, client, query):
        assert client.get(f"/v1/ledger/export?{query}").status_code == 400

    def test_audit(self, client, filled):
        event, actors = filled
        body = client.get(f"/v1/events/{event.id}/audit").json()
        assert [e["action"] for e in body] == ["FLAGGED", "VIEW", "VIEW", "VIEW", "VIEW"]
        assert body[0]["actor_id"] is None and body[1]["actor_id"] == str(actors[0])
        assert body[1]["timestamp"] == "2026-09-27T20:02:00Z"
        assert set(body[0]) == {"actor_id", "action", "timestamp", "seq"}

    @pytest.mark.parametrize("event_id", [str(uuid4()), "not-a-uuid"])
    def test_audit_unknown_event_is_404(self, client, event_id):
        assert client.get(f"/v1/events/{event_id}/audit").status_code == 404

    def test_system_key_is_403(self, client):
        client.app.dependency_overrides[get_principal] = lambda: system_principal(uuid4())
        assert client.get("/v1/ledger/verify").status_code == 403
        assert client.get("/v1/ledger/export").status_code == 403


def test_view_through_the_api_adds_a_chained_entry(uow):
    """REQ-14 end to end: GET /v1/events/{id} writes a VIEW entry with the viewer's id."""
    from app.modules.ledger.interface import get_ledger

    clock = FixedClock(datetime(2026, 10, 1, tzinfo=UTC))
    system_id = uow.seed_system()
    event = make_event(system_id)
    uow.events.rows[event.id] = event
    viewer = user_principal(Role.VIEWER)
    app = create_app()
    app.dependency_overrides.update(
        {
            get_principal: lambda: viewer,
            get_uow: lambda: uow,
            get_ledger: lambda: ChainLedger(uow.ledger, clock),
        }
    )
    client = TestClient(app)
    assert client.get(f"/v1/events/{event.id}").status_code == 200
    (entry,) = uow.ledger.entries
    assert (entry.entry_type, entry.actor_id, entry.event_id) == (
        LedgerEntryType.VIEW,
        viewer.actor_id,
        event.id,
    )
    assert entry.at == clock.now()
