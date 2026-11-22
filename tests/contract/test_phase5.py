"""Phase 5 contract checks: reports (REQ-12), roles on them (REQ-13), the risk indicator
(REQ-15), retention and erasure (REQ-05, REQ-16), and the agreed contract delta's report
versions, submissions, PDF and verification runs (Δ1–Δ6).

Black box, through the pod interface only. Each test runs when the target lists phase 5 in
GET /v1/meta and a reviewer key is configured; otherwise it is skipped with the reason. Tests
that move the simulated clock need the test routes. The erasure test moves it six months, so it
is the last test of the run (this module sorts after test_contract.py).
"""

import hashlib
import json
from datetime import timedelta
from pathlib import Path

import jsonschema_rs
import pytest

from tests.contract.scenarios import assert_error_body, parse
from tests.contract.test_contract import advance_clock, export_all, send

covers = pytest.mark.covers
SCHEMA = jsonschema_rs.validator_for(
    json.loads(
        (Path(__file__).resolve().parents[2] / "docs/pod-interface/report.schema.json").read_text(
            encoding="utf-8"
        )
    )
)
# All four Art. 3(49) fields, in the disposition body's names (Phase 2 §5.2).
HARM_FIELDS = [
    "health_safety",
    "critical_infrastructure",
    "fundamental_rights",
    "property_environment",
]
EVIDENCE = "Applicants with thin credit files were refused; contract-suite evidence."
RATIONALE = "Credit decisions changed for real applicants; contract-suite rationale."
# A deadline or risk indicator may be off by the time a request takes.
TOLERANCE_S = 60


@pytest.fixture(scope="module")
def phase5(api, meta_phases):
    if 5 not in meta_phases:
        pytest.skip(f"the target lists phases {meta_phases} in GET /v1/meta, not 5")
    if "reviewer" not in api.target.keys:
        pytest.skip("set CONTRACT_REVIEWER_KEY to run the Phase 5 checks")
    return api


def flag(api) -> str:
    resp, _ = send(api, round(api.target.drift_limit + 0.05, 6))
    assert resp.status_code == 201, resp.text
    event_id = resp.json()["flagged_event_id"]
    assert event_id, resp.json()
    return event_id


def dispose(api, event_id, tier="OTHER_SERIOUS", *, evidence=EVIDENCE, rationale=None):
    body = {
        "tier": tier,
        "harm_fields": {
            name: (
                {"status": "CONFIRMED", "evidence": evidence}
                if name == "fundamental_rights"
                else {"status": "NOT_APPLICABLE"}
            )
            for name in HARM_FIELDS
        },
    }
    if rationale is not None:
        body["rationale"] = rationale
    resp = api.call("POST", f"/v1/events/{event_id}/disposition", as_="reviewer", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def report(api, event_id, *, as_="reviewer", **params):
    return api.call("GET", f"/v1/events/{event_id}/report", as_=as_, params=params or None)


@pytest.fixture(scope="module")
def reported(phase5):
    """A serious event with its report assembled: (event_id, report response)."""
    event_id = flag(phase5)
    dispose(phase5, event_id)
    resp = report(phase5, event_id)
    assert resp.status_code == 200, resp.text
    return event_id, resp


def versions(api, event_id):
    resp = api.call("GET", f"/v1/events/{event_id}/reports", as_="viewer")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list), resp.json()  # a bare array (Δ4)
    return resp.json()


# --- REQ-12 / AC-13: the report -------------------------------------------------------------------


@covers(
    reqs=["REQ-12"],
    acs=["AC-13"],
    endpoints=["POST /v1/events/{id}/disposition", "GET /v1/events/{id}/report"],
    model1=["DISPOSITIONED"],
)
def test_ac13_report_follows_the_shared_schema(reported):
    event_id, resp = reported
    body = resp.json()
    problems = [f"{e.instance_path}: {e.message}" for e in SCHEMA.iter_errors(body)]
    assert problems == [], problems
    times = body["timestamps"]
    for name in ("detected_at", "determined_at", "assembled_at"):
        assert times[name] is not None, name
    # The ordering JSON Schema can't express.
    assert parse(times["detected_at"]) < parse(times["determined_at"])
    assert parse(times["determined_at"]) <= parse(times["assembled_at"])
    assert body["event"]["event_id"] == event_id
    assert body["report_kind"] == "SERIOUS_INCIDENT"
    rights = body["harm_fields"]["fundamental_rights_infringement"]
    assert (rights["status"], rights["evidence"]) == ("CONFIRMED", EVIDENCE)


@covers(reqs=["REQ-12"], acs=["AC-13"], endpoints=["GET /v1/events/{id}/report"])
def test_ac13_report_headers_and_bytes_are_stable(phase5, reported):
    event_id, first = reported
    again = report(phase5, event_id)
    assert again.status_code == 200 and again.content == first.content
    sha = hashlib.sha256(first.content).hexdigest()
    assert first.headers["x-report-sha256"] == sha  # Δ2: the body's hash
    assert first.headers["x-report-id"] == first.json()["report_id"]
    assert first.headers["x-report-version"] == str(first.json()["report_version"])
    assert first.headers["etag"].strip('"') == sha


@covers(reqs=["REQ-12"], acs=["AC-13"], endpoints=["GET /v1/events/{id}/report"])
def test_report_pdf_is_the_same_report(phase5, reported):
    """Δ2: format=pdf is the PDF of the same stored report."""
    event_id, as_json = reported
    pdf = report(phase5, event_id, format="pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content.startswith(b"%PDF-")
    for name in ("x-report-id", "x-report-version", "x-report-sha256"):
        assert pdf.headers[name] == as_json.headers[name], name
    bad = report(phase5, event_id, format="xml")
    assert bad.status_code == 400 and bad.json().get("error_code") == "MALFORMED_QUERY"


@covers(reqs=["REQ-08a", "REQ-12"], acs=["AC-08"], endpoints=["GET /v1/events/{id}/report"])
def test_ac08_no_report_before_a_disposition(phase5):
    event_id = flag(phase5)
    resp = report(phase5, event_id)
    assert resp.status_code == 409, resp.text
    assert_error_body(resp)
    assert resp.json().get("error_code") == "NO_DISPOSITION"  # Δ2


# --- REQ-13 / AC-14: only reviewers ---------------------------------------------------------------


@covers(
    reqs=["REQ-13"],
    acs=["AC-14"],
    endpoints=["GET /v1/events/{id}/report", "POST /v1/events/{id}/disposition"],
    model1=["DENIED"],
)
@pytest.mark.parametrize("role", ["viewer", "admin", "system"])
def test_ac14_non_reviewers_get_no_report_and_assign_no_tier(phase5, reported, role):
    event_id, _ = reported
    resp = report(phase5, event_id, as_=role)
    assert resp.status_code == 403, resp.text
    assert_error_body(resp)
    tier = phase5.call(
        "POST", f"/v1/events/{event_id}/disposition", as_=role, json={"tier": "DEATH"}
    )
    assert tier.status_code == 403, tier.text


# --- Δ3 / Δ4: report versions and submissions ---------------------------------------------------


@covers(
    reqs=["REQ-12"],
    endpoints=["GET /v1/events/{id}/reports", "GET /v1/reports/{id}"],
)
def test_report_versions_and_reading_one_by_id(phase5, reported):
    event_id, current = reported
    (version,) = versions(phase5, event_id)
    assert version["report_id"] == current.headers["x-report-id"]
    assert version["status"] == "ASSEMBLED"
    by_id = phase5.call("GET", f"/v1/reports/{version['report_id']}", as_="reviewer")
    assert by_id.status_code == 200 and by_id.content == current.content
    assert (
        phase5.call("GET", f"/v1/reports/{version['report_id']}", as_="viewer").status_code == 403
    )


@covers(reqs=["REQ-12"], endpoints=["POST /v1/events/{id}/report/submission"])
def test_a_submission_is_recorded_once_for_the_current_version(phase5, reported):
    event_id, current = reported
    path = f"/v1/events/{event_id}/report/submission"
    body = {
        "report_id": current.headers["x-report-id"],
        "json_sha256": "0" * 64,
        "report_type": "Initial",
    }
    wrong = phase5.call("POST", path, as_="reviewer", json=body)
    assert wrong.status_code == 409 and wrong.json().get("error_code") == "REPORT_HASH_MISMATCH"
    assert phase5.call("POST", path, as_="viewer", json=body).status_code == 403
    body["json_sha256"] = hashlib.sha256(current.content).hexdigest()
    ok = phase5.call("POST", path, as_="reviewer", json=body)
    assert ok.status_code == 201, ok.text
    out = ok.json()
    assert (out["report_id"], out["submission_kind"]) == (body["report_id"], "INITIAL")
    again = phase5.call("POST", path, as_="reviewer", json=body)
    assert again.status_code == 409
    assert again.json().get("error_code") == "REPORT_ALREADY_SUBMITTED"
    (version,) = versions(phase5, event_id)
    assert version["status"] == "SUBMITTED"
    assert report(phase5, event_id).content == current.content  # the report never changes


# --- REQ-15 / AC-17: the risk indicator -----------------------------------------------------------


@covers(
    reqs=["REQ-15", "REQ-10"],
    acs=["AC-17", "AC-11"],
    endpoints=[
        "POST /v1/test/clock/advance",
        "POST /v1/events/{id}/disposition",
        "GET /v1/events/{id}",
        "GET /v1/events/{id}/report",
    ],
    model4=["INV-13"],
)
def test_ac17_detection_and_deadline_visible_with_the_gap_between(phase5, test_routes):
    if not test_routes:
        pytest.skip("needs POST /v1/test/clock/advance (test routes)")
    event_id = flag(phase5)
    detected = phase5.call("GET", f"/v1/events/{event_id}", as_="viewer").json()["detected_at"]
    advance_clock(phase5, 4 * 3600)  # assign severity four hours later
    event = dispose(phase5, event_id)
    assert event["detected_at"] == detected  # set once (B-15)
    assigned = parse(event["revision_history"][-1]["recorded_at"])
    # B-10: OTHER_SERIOUS, A + 15 days.
    assert abs((parse(event["deadline_utc"]) - (assigned + timedelta(days=15))).total_seconds()) < 1
    body = report(phase5, event_id).json()
    gap = body["risk_indicator"]["detection_to_assignment_seconds"]
    assert abs(gap - 4 * 3600) <= TOLERANCE_S, gap
    assert parse(body["timestamps"]["detected_at"]) == parse(detected)


# --- Δ5: ledger verification runs ----------------------------------------------------------------


@covers(reqs=["REQ-06"], endpoints=["POST /v1/ledger/verifications"], model4=["INV-7"])
def test_a_reviewer_runs_a_ledger_verification_and_readers_list_it(phase5):
    run = phase5.call("POST", "/v1/ledger/verifications", as_="reviewer")
    assert run.status_code == 201, run.text
    assert run.json()["valid"] is True and run.json()["trigger"] == "MANUAL"
    listed = phase5.call("GET", "/v1/ledger/verifications", as_="viewer")
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["verification_id"] == run.json()["verification_id"]  # newest first
    assert phase5.call("POST", "/v1/ledger/verifications", as_="viewer").status_code == 403


# --- REQ-05 / AC-05, REQ-16 / AC-18: retention and erasure (moves the clock six months: last) -----


@covers(
    reqs=["REQ-05", "REQ-16", "REQ-06"],
    acs=["AC-05", "AC-18"],
    endpoints=[
        "POST /v1/test/clock/advance",
        "GET /v1/events/{id}",
        "GET /v1/events/{id}/report",
        "GET /v1/ledger/export",
        "GET /v1/ledger/verify",
    ],
    model4=["INV-7", "INV-10"],
)
def test_ac05_ac18_kept_until_retention_then_erased_with_the_chain_intact(phase5, test_routes):
    if not test_routes:
        pytest.skip("needs POST /v1/test/clock/advance (test routes)")
    event_id = flag(phase5)
    dispose(phase5, event_id, rationale=RATIONALE)
    detail = phase5.call("GET", f"/v1/events/{event_id}", as_="viewer").json()
    retain_until = parse(detail["retain_until"])
    detected = parse(detail["detected_at"])
    now = parse(phase5.call("GET", "/v1/test/clock", as_="admin").json()["simulated_time"])

    # AC-05: still retrievable, personal data intact, one day before six months.
    advance_clock(phase5, int((retain_until - timedelta(days=1) - now).total_seconds()))
    kept = phase5.call("GET", f"/v1/events/{event_id}", as_="viewer")
    assert kept.status_code == 200, kept.text
    assert kept.json()["harm_fields"]["fundamental_rights"]["evidence"] == EVIDENCE
    early = phase5.call("POST", f"/v1/events/{event_id}/erasure", as_="reviewer")
    assert early.status_code == 409 and early.json().get("error_code") == "RETENTION_NOT_EXPIRED"
    assert retain_until - detected >= timedelta(days=180)  # INV-10: at least six months

    # AC-18: past retention, erase; the ledger keeps every entry's position and hash.
    before = export_all(phase5)
    advance_clock(phase5, 86400 + 1)
    erased = phase5.call("POST", f"/v1/events/{event_id}/erasure", as_="reviewer")
    assert erased.status_code == 200, erased.text
    assert erased.json()["erased_fields"], erased.json()
    after = phase5.call("GET", f"/v1/events/{event_id}", as_="viewer").json()
    assert after["harm_fields"]["fundamental_rights"]["evidence"] is None
    assert all(r["rationale"] is None for r in after["revision_history"])
    gone = report(phase5, event_id)
    assert gone.status_code == 410 and gone.json().get("error_code") == "RECORD_ERASED"
    chain = export_all(phase5)
    assert [(e["seq"], e["entry_hash"]) for e in chain[: len(before)]] == [
        (e["seq"], e["entry_hash"]) for e in before
    ]
    verify = phase5.call("GET", "/v1/ledger/verify", as_="viewer")
    assert verify.status_code == 200 and verify.json()["valid"] is True, verify.text
    again = phase5.call("POST", f"/v1/events/{event_id}/erasure", as_="reviewer")
    assert again.status_code == 409 and again.json().get("error_code") == "ALREADY_ERASED"
