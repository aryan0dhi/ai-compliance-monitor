"""Hosted cross-team tester (app/tester): allowlist, one run at a time, keys never returned."""

import threading

import pytest
from fastapi.testclient import TestClient

from app.tester.main import RunIn, Tester, create_tester_app, origin

REPORT = {
    "summary": {"passed": 2, "failed": 0, "skipped": 0},
    "coverage": {"requirements": {"ratio": "1/8"}},
    "tests": [{"test": "test_a", "outcome": "passed"}],
}
BODY = {
    "target_url": "https://34-57-175-31.sslip.io/",
    "system_key": "acm_secret-system",
    "viewer_key": "acm_secret-viewer",
    "admin_key": "acm_secret-admin",
}


@pytest.fixture
def harness():
    seen: list[dict] = []
    release = threading.Event()

    def runner(env, timeout_s):
        seen.append(env)
        release.wait(5)
        return "finished", 0, REPORT, "# report\n", "2 passed"

    tester = Tester(
        allowed={"https://34-57-175-31.sslip.io", "https://eu-ai-act-monitor-api.fly.dev"},
        public_url="https://34-57-175-31.sslip.io/tester",
        runner=runner,
    )
    return TestClient(create_tester_app(tester)), tester, seen, release


def wait_finished(client, run_id):
    for _ in range(100):
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] != "running":
            return body
        threading.Event().wait(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Example.com/", "https://example.com"),
        ("https://example.com:443/x", "https://example.com"),
        ("http://example.com:8080", "http://example.com:8080"),
        ("ftp://example.com", None),
        ("https://", None),
        ("https://example.com:99999", None),
    ],
)
def test_origin(url, expected):
    assert origin(url) == expected


def test_run_lifecycle_and_keys_never_returned(harness):
    client, tester, seen, release = harness
    started = client.post("/runs", json=BODY)
    assert started.status_code == 202
    run_id = started.json()["run_id"]
    release.set()
    body = wait_finished(client, run_id)
    assert body["status"] == "finished" and body["summary"] == REPORT["summary"]
    assert body["coverage"] == REPORT["coverage"] and body["tests"] == REPORT["tests"]
    assert client.get(f"/runs/{run_id}/report").text == "# report\n"
    every_response = (
        started.text
        + client.get(f"/runs/{run_id}").text
        + client.get("/runs").text
        + client.get("/").text
    )
    assert "acm_secret" not in every_response


def test_keys_and_receiver_reach_the_test_process(harness):
    client, tester, seen, release = harness
    release.set()
    run_id = client.post("/runs", json={**BODY, "system_id": "sys-42", "drift_limit": 0.5}).json()[
        "run_id"
    ]
    wait_finished(client, run_id)
    (env,) = seen
    assert env["CONTRACT_BASE_URL"] == "https://34-57-175-31.sslip.io"
    assert (env["CONTRACT_SYSTEM_KEY"], env["CONTRACT_ADMIN_KEY"]) == (
        "acm_secret-system",
        "acm_secret-admin",
    )
    assert (env["CONTRACT_SYSTEM_ID"], env["CONTRACT_DRIFT_LIMIT"]) == ("sys-42", "0.5")
    assert env["CONTRACT_RECEIVER_URL"] == f"https://34-57-175-31.sslip.io/tester/hook/{run_id}"
    assert "POSTGRES_PASSWORD" not in env and "DATABASE_URL" not in env


def test_webhooks_off_without_a_public_url():
    tester = Tester(allowed={"https://a.example"}, public_url=None)
    env = tester.environment(RunIn(**{**BODY, "target_url": "https://a.example"}), "r1")
    assert env["CONTRACT_WEBHOOKS"] == "off" and "CONTRACT_RECEIVER_URL" not in env


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example",
        "http://169.254.169.254",
        "https://34-57-175-31.sslip.io.evil.example",
    ],
)
def test_only_allowlisted_targets(harness, target):
    client, tester, seen, release = harness
    resp = client.post("/runs", json={**BODY, "target_url": target})
    assert resp.status_code == 400 and seen == [] and tester.runs == {}


def test_one_run_at_a_time(harness):
    client, tester, seen, release = harness
    first = client.post("/runs", json=BODY)
    second = client.post(
        "/runs", json={**BODY, "target_url": "https://eu-ai-act-monitor-api.fly.dev"}
    )
    assert (first.status_code, second.status_code) == (202, 409)
    release.set()
    wait_finished(client, first.json()["run_id"])
    assert client.post("/runs", json=BODY).status_code == 202


def test_runner_crash_is_reported_and_frees_the_slot():
    def broken(env, timeout_s):
        raise RuntimeError("pytest missing")

    tester = Tester(allowed={"https://a.example"}, public_url=None, runner=broken)
    client = TestClient(create_tester_app(tester))
    run_id = client.post("/runs", json={**BODY, "target_url": "https://a.example"}).json()["run_id"]
    body = wait_finished(client, run_id)
    assert body["status"] == "error" and "pytest missing" in body["log_tail"]
    assert client.get(f"/runs/{run_id}/report").status_code == 409
    assert client.post("/runs", json={**BODY, "target_url": "https://a.example"}).status_code == 202


def test_index_and_unknown_run(harness):
    client, *_ = harness
    index = client.get("/").json()
    assert index["allowed_targets"] == [
        "https://34-57-175-31.sslip.io",
        "https://eu-ai-act-monitor-api.fly.dev",
    ]
    assert client.get("/runs/nope").status_code == 404
