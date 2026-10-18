"""Section 5 contract checks for Phase 3 (black box; configuration in conftest.py).

Checks that depend on a decision the pod hasn't agreed yet name it (decision="D-xx"), so a
failure against a podmate's service reads as "open decision", not "defect".
"""

import copy
import importlib.util
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

covers = pytest.mark.covers
VERIFIER = Path(__file__).resolve().parents[2] / "scripts" / "verify_ledger_export.py"
UNKNOWN_EVENT = "00000000-0000-4000-8000-000000000000"
GAP_SETTLE_S = float(os.environ.get("CONTRACT_GAP_SETTLE_S", "12"))


def offline_verifier():
    spec = importlib.util.spec_from_file_location("verify_ledger_export", VERIFIER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_SEQ = iter(range(1, 10**9))


def sample_time() -> str:
    # Unique per call, so repeats never collide on (system_id, metric_name, timestamp).
    return (datetime.now(UTC) + timedelta(microseconds=next(_SEQ))).isoformat()


def send(api, value, *, drift=True, as_="system", system_id=None, timestamp=None):
    t = api.target
    body = {
        "system_id": system_id or t.system_id,
        "metric_name": t.drift_metric if drift else t.perf_metric,
        "metric_type": "drift" if drift else "performance",
        "value": value,
        "timestamp": timestamp or sample_time(),
    }
    return api.call("POST", "/v1/metrics", as_=as_, json=body), body


def items(page):
    """Section 5 says "paginated array"; accept a bare list or an {"items": [...]} envelope."""
    return page if isinstance(page, list) else page["items"]


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


@pytest.fixture(scope="module")
def breach(api, test_routes):
    resp, body = send(api, round(api.target.drift_limit + 0.05, 6))
    assert resp.status_code == 201, resp.text
    return resp.json()["flagged_event_id"], body, time.monotonic()


# --- REQ-13: authentication and roles ------------------------------------------------------------


@covers(
    reqs=["REQ-13"],
    endpoints=[
        "POST /v1/metrics",
        "GET /v1/events",
        "GET /v1/events/{id}",
        "GET /v1/events/{id}/audit",
        "GET /v1/ledger/export",
        "GET /v1/ledger/verify",
        "POST /v1/webhooks",
    ],
)
@pytest.mark.parametrize("header", [None, "Bearer not-a-real-key"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/metrics"),
        ("GET", "/v1/events"),
        ("GET", f"/v1/events/{UNKNOWN_EVENT}"),
        ("GET", f"/v1/events/{UNKNOWN_EVENT}/audit"),
        ("GET", "/v1/ledger/export"),
        ("GET", "/v1/ledger/verify"),
        ("POST", "/v1/webhooks"),
    ],
)
def test_requests_without_a_valid_key_are_401(api, method, path, header):
    headers = {"Authorization": header} if header else {}
    resp = api.call(method, path, as_=None, headers=headers, json={})
    assert resp.status_code == 401, resp.text


@covers(reqs=["REQ-13"], endpoints=["POST /v1/metrics", "GET /v1/events"], model1=["DENIED"])
def test_roles_are_enforced(api):
    assert send(api, 0.1, as_="viewer")[0].status_code == 403  # humans don't submit metrics
    other = str(uuid.uuid4())
    assert send(api, 0.1, system_id=other)[0].status_code == 403  # key scoped to its system
    assert api.call("GET", "/v1/events", as_="system").status_code == 403


@covers(reqs=["REQ-13"], endpoints=["POST /v1/webhooks"], model1=["DENIED"], decision="D-13")
def test_only_admins_register_webhooks(api):
    body = {"url": "https://example.com/hook", "event_types": ["flag_notification"]}
    assert api.call("POST", "/v1/webhooks", as_="viewer", json=body).status_code == 403


# --- REQ-01 / REQ-03: ingestion and flagging -----------------------------------------------------


@covers(reqs=["REQ-01"], acs=["AC-01"], endpoints=["POST /v1/metrics"])
def test_ac01_valid_samples_stored_and_malformed_rejected(api, test_routes):
    resp, body = send(api, round(api.target.drift_limit / 2, 6))
    assert resp.status_code == 201, resp.text
    assert "flagged_event_id" not in resp.json() or resp.json()["flagged_event_id"] is None
    for broken in ({"value": None}, {"timestamp": "yesterday"}, {"metric_type": "latency"}):
        bad = {k: v for k, v in {**body, **broken}.items() if v is not None}
        bad["timestamp"] = broken.get("timestamp", sample_time())
        assert api.call("POST", "/v1/metrics", as_="system", json=bad).status_code == 400


@covers(
    reqs=["REQ-03", "REQ-05"],
    acs=["AC-03"],
    endpoints=["POST /v1/metrics", "GET /v1/events/{id}"],
    model1=["FLAGGED"],
    model4=["INV-10"],
)
def test_ac03_drift_breach_records_every_required_field(api, breach):
    event_id, body, _ = breach
    event = api.call("GET", f"/v1/events/{event_id}", as_="viewer")
    assert event.status_code == 200, event.text
    e = event.json()
    assert e["event_id"] == event_id and e["system_id"] == body["system_id"]
    assert (e["type"], e["state"]) == ("threshold_breach", "FLAGGED")
    assert e["metric_name"] == body["metric_name"]
    assert parse(e["metric_timestamp"]) == parse(body["timestamp"])
    assert e["observed_value"] == pytest.approx(body["value"])
    assert e["threshold_value"] == pytest.approx(api.target.drift_limit)
    detected = parse(e["detected_at"])
    if "retain_until" in e:  # INV-10: at least six months
        assert parse(e["retain_until"]) >= detected + timedelta(days=181)


@covers(reqs=["REQ-03"], acs=["AC-03"], endpoints=["POST /v1/metrics"], decision="D-01")
def test_ac03_performance_metric_breaches_below_its_threshold(api, test_routes):
    resp, _ = send(api, round(api.target.perf_limit - 0.05, 6), drift=False)
    assert resp.status_code == 201 and resp.json().get("flagged_event_id"), resp.text


@covers(reqs=["REQ-03"], acs=["AC-03"], endpoints=["POST /v1/metrics"])
@pytest.mark.parametrize("drift", [True, False], ids=["drift", "performance"])
def test_ac03_value_exactly_at_threshold_does_not_flag(api, test_routes, drift):
    limit = api.target.drift_limit if drift else api.target.perf_limit
    resp, _ = send(api, limit, drift=drift)
    assert resp.status_code == 201, resp.text
    assert not resp.json().get("flagged_event_id")


@covers(reqs=["REQ-03"], endpoints=["GET /v1/events"], decision="D-06")
def test_listing_filters_by_system_and_type(api, breach):
    event_id, body, _ = breach
    query = {"system_id": body["system_id"], "type": "threshold_breach"}
    page = api.call("GET", "/v1/events", as_="viewer", params=query)
    assert page.status_code == 200, page.text
    assert event_id in {e["event_id"] for e in items(page.json())}
    none = api.call("GET", "/v1/events", as_="viewer", params={"system_id": str(uuid.uuid4())})
    assert items(none.json()) == []


# --- REQ-14 / REQ-06: audit trail and ledger -----------------------------------------------------


@covers(
    reqs=["REQ-14"],
    acs=["AC-15"],
    endpoints=["GET /v1/events/{id}", "GET /v1/events/{id}/audit"],
    model4=["INV-8", "INV-13"],
)
def test_ac15_every_view_is_logged_with_identity_and_time(api, breach):
    event_id, _, _ = breach
    before = api.call("GET", f"/v1/events/{event_id}/audit", as_="viewer").json()
    first = api.call("GET", f"/v1/events/{event_id}", as_="viewer").json()
    second = api.call("GET", f"/v1/events/{event_id}", as_="viewer").json()
    assert first["detected_at"] == second["detected_at"]  # INV-13: set once
    after = api.call("GET", f"/v1/events/{event_id}/audit", as_="viewer").json()
    new = after[len(before) :]
    assert [a["action"] for a in new] == ["VIEW", "VIEW"]
    assert all(a["actor_id"] and a["timestamp"] for a in new)
    assert any(a["action"] == "FLAGGED" for a in after)


def export_all(api) -> list[dict]:
    entries, after = [], 0
    while after is not None:
        page = api.call("GET", "/v1/ledger/export", as_="viewer", params={"after_seq": after})
        assert page.status_code == 200, page.text
        body = page.json()
        entries += body["entries"]
        after = body.get("next_after_seq")
    return entries


@covers(
    reqs=["REQ-06"],
    acs=["AC-06"],
    endpoints=["GET /v1/ledger/verify", "GET /v1/ledger/export"],
    model4=["INV-7"],
    decision="D-14",
)
def test_ac06_ledger_verifies_and_an_edited_export_is_caught_offline(api, breach):
    verdict = api.call("GET", "/v1/ledger/verify", as_="viewer")
    assert verdict.status_code == 200 and verdict.json()["valid"] is True, verdict.text
    assert verdict.json()["first_mismatch"] is None
    entries = export_all(api)
    verifier = offline_verifier()
    assert verifier.verify(entries)["valid"] is True  # independent re-verification (Q4.3)
    tampered = copy.deepcopy(entries)
    tampered[len(tampered) // 2]["payload"] = {"tampered": True}
    assert verifier.verify(tampered)["valid"] is False


# --- REQ-04: reviewer notification ---------------------------------------------------------------


@covers(reqs=["REQ-04"], acs=["AC-04"], endpoints=["POST /v1/webhooks"])
def test_ac04_breach_notification_reaches_the_webhook_within_15_minutes(api, receiver, breach):
    if receiver.url is None:
        pytest.skip("CONTRACT_WEBHOOKS=off")
    event_id, _, flagged_at = breach

    def mentions_event(body: dict) -> bool:
        return event_id in (body.get("event_id"), body.get("event", {}).get("event_id"))

    def delivered():
        return next((b for b in receiver.bodies() if mentions_event(b)), None)

    body = api.wait_for(delivered, f"a webhook notification for {event_id}")
    assert time.monotonic() - flagged_at <= 15 * 60
    assert body.get("notification_type", "flag_notification") == "flag_notification"


# --- REQ-02: monitoring gaps (needs the test clock) ----------------------------------------------


def gap_ids(api) -> dict[str, dict]:
    page = api.call(
        "GET",
        "/v1/events",
        as_="viewer",
        params={"type": "monitoring_gap", "system_id": api.target.system_id},
    )
    assert page.status_code == 200, page.text
    return {e["event_id"]: e for e in items(page.json())}


def open_gap(api) -> dict:
    """Send one sample, then withhold data until a gap opens. Checks the 2-interval boundary."""
    interval = api.target.interval_s
    known = set(gap_ids(api))
    assert send(api, 0.1)[0].status_code == 201  # last data now

    def advance(seconds):
        resp = api.call("POST", "/v1/test/clock/advance", as_="admin", json={"seconds": seconds})
        assert resp.status_code == 200, resp.text

    # Boundary: withholding 2 intervals must NOT trigger.
    advance(2 * interval)
    time.sleep(GAP_SETTLE_S)
    assert set(gap_ids(api)) == known

    # 3 intervals: a gap event appears.
    advance(interval)

    def new_gap():
        return next((g for i, g in gap_ids(api).items() if i not in known), None)

    gap = api.wait_for(new_gap, "a gap event")
    assert gap["state"] not in ("UNDER_REVIEW", "DISPOSITIONED")  # INV-12
    return gap


def close_gap(api, gap: dict) -> None:
    """Data resumes: the gap closes with its duration (Ex-3)."""
    assert send(api, 0.1)[0].status_code == 201
    api.wait_for(lambda: gap_ids(api)[gap["event_id"]]["state"] == "GAP_CLOSED", "GAP_CLOSED")
    detail = api.call("GET", f"/v1/events/{gap['event_id']}", as_="viewer").json()
    if "gap_duration_seconds" in detail:
        assert detail["gap_duration_seconds"] >= 3 * api.target.interval_s


@covers(
    reqs=["REQ-02"],
    acs=["AC-02"],
    endpoints=["POST /v1/test/clock/advance", "POST /v1/test/reset", "GET /v1/events"],
    model1=["GAP_OPEN", "GAP_CLOSED"],
    model4=["INV-12"],
    decision="D-12",
)
def test_ac02_gap_after_three_missed_intervals_not_two(api, test_routes):
    if not test_routes:
        pytest.skip("service has no /v1/test routes (time-dependent AC-02 needs the test clock)")
    close_gap(api, open_gap(api))


@covers(
    reqs=["REQ-02", "REQ-04"],
    acs=["AC-02"],
    endpoints=["POST /v1/webhooks", "POST /v1/test/clock/advance"],
    model1=["REVIEWER_NOTIFIED"],
    decision="D-12",
)
def test_ac02_gap_notification_reaches_the_reviewer(api, receiver, test_routes):
    if not test_routes:
        pytest.skip("service has no /v1/test routes (time-dependent AC-02 needs the test clock)")
    if receiver.url is None:
        pytest.skip("CONTRACT_WEBHOOKS=off")
    gap = open_gap(api)
    api.wait_for(
        lambda: any(b.get("event_id") == gap["event_id"] for b in receiver.bodies()),
        "the gap notification webhook",
    )
    api.wait_for(
        lambda: gap_ids(api)[gap["event_id"]]["state"] == "REVIEWER_NOTIFIED",
        "state REVIEWER_NOTIFIED",
    )
    close_gap(api, gap)


@covers(reqs=["REQ-06"], endpoints=["GET /v1/ledger/verify"], model4=["INV-7"])
def test_ledger_still_verifies_after_the_whole_run(api):
    assert api.call("GET", "/v1/ledger/verify", as_="viewer").json()["valid"] is True
