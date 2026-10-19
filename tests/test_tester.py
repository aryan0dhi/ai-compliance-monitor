"""Tester service (tester/): the course testing interface's tester side.

The runner tests run real pytest subprocesses over small throwaway suites; the app tests use a
mock target (httpx.MockTransport) and a scripted runner.
"""

import json
import textwrap
import uuid
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema_rs import Draft202012Validator

from tester import main as tester_main
from tester.runner import PlannedTest, SuiteRun, assemble, run_suite
from tests.contract.scope import ENDPOINTS, PHASE3_ACS

ROOT = Path(__file__).resolve().parents[1]
DOC = yaml.safe_load(
    (ROOT / "docs" / "testing-interface" / "tester-openapi.yaml").read_text(encoding="utf-8")
)
TARGET = "http://system.test"


def conforms(body, schema_name: str) -> None:
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": DOC["components"]}
    Draft202012Validator(schema).validate(body)


def report_problems(report: dict) -> list[str]:
    """The interface's report rules beyond the schema."""
    problems = []
    ids = [r["test_id"] for r in report["results"]]
    if len(ids) != len(set(ids)):
        problems.append("duplicate test_id")
    for name, entry in report["coverage"].items():
        covered, total = entry["covered"], entry["total"]
        if (covered is None) != (total is None):
            problems.append(f"{name}: covered and total must both be null or both numbers")
        elif covered is not None and covered > total:
            problems.append(f"{name}: covered > total")
    return problems


# --- runner (real pytest subprocesses) ----------------------------------------------------------

SUITE = """
    import os
    import time

    import httpx
    import pytest


    class TargetDown(Exception):
        error_source = "system"


    @pytest.mark.covers(endpoints=["POST /v1/metrics"], acs=["AC-01"])
    def test_pass():
        pass


    @pytest.mark.covers(endpoints=["GET /v1/events"], acs=["AC-02"])
    def test_fail():
        assert 1 == 2, "expected one to be two"


    def test_skip():
        pytest.skip("no webhook receiver")


    def test_timeout():
        raise httpx.ConnectTimeout("timed out")


    def test_target_5xx():
        raise TargetDown("GET /v1/events -> 503")


    def test_bug():
        raise KeyError("oops")


    @pytest.fixture
    def broken():
        raise RuntimeError("fixture broke")


    def test_fixture(broken):
        pass


    @pytest.mark.parametrize("path", ["GET /v1/events", "a/b"])
    def test_param(path):
        pass
"""


def write_suite(tmp_path: Path, source: str) -> Path:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_suite.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return suite


def test_runner_maps_outcomes_and_sources(tmp_path):
    run = run_suite(TARGET, suite=write_suite(tmp_path, SUITE), timeout_s=120)
    assert run.aborted is None
    by_id = {r["test_id"]: r for r in run.results}
    assert list(by_id) == [
        "test_suite.py::test_pass",
        "test_suite.py::test_fail",
        "test_suite.py::test_skip",
        "test_suite.py::test_timeout",
        "test_suite.py::test_target_5xx",
        "test_suite.py::test_bug",
        "test_suite.py::test_fixture",
        "test_suite.py::test_param[GET /v1/events]",
        "test_suite.py::test_param[a/b]",
    ]
    outcome = {k: (r["outcome"], r.get("source")) for k, r in by_id.items()}
    assert outcome["test_suite.py::test_pass"] == ("pass", None)
    assert outcome["test_suite.py::test_fail"] == ("fail", None)
    assert outcome["test_suite.py::test_skip"] == ("skip", None)
    assert outcome["test_suite.py::test_timeout"] == ("error", "communication")
    assert outcome["test_suite.py::test_target_5xx"] == ("error", "system")
    assert outcome["test_suite.py::test_bug"] == ("error", "tester")
    assert outcome["test_suite.py::test_fixture"] == ("error", "tester")
    assert "expected one to be two" in by_id["test_suite.py::test_fail"]["message"]
    assert by_id["test_suite.py::test_skip"]["message"] == "no webhook receiver"
    assert all(r["message"] for r in run.results)
    tags = {p.test_id: (p.endpoints, p.acs) for p in run.planned}
    assert tags["test_suite.py::test_pass"] == (["POST /v1/metrics"], ["AC-01"])


def test_runner_abort_keeps_results_so_far(tmp_path):
    suite = write_suite(
        tmp_path,
        """
        import os

        def test_first():
            pass

        def test_crash():
            os._exit(3)

        def test_never():
            pass
        """,
    )
    run = run_suite(TARGET, suite=suite, timeout_s=120)
    assert run.aborted and "exit code 3" in run.aborted
    assert [(r["outcome"], r.get("source")) for r in run.results] == [
        ("pass", None),
        ("error", "tester"),
        ("skip", None),
    ]


def test_runner_timeout_is_an_abort(tmp_path):
    suite = write_suite(
        tmp_path,
        """
        import time

        def test_slow():
            time.sleep(120)
        """,
    )
    run = run_suite(TARGET, suite=suite, timeout_s=10)
    assert run.aborted and "exceeded 10 s" in run.aborted
    assert [(r["outcome"], r.get("source")) for r in run.results] == [("error", "tester")]


def test_runner_passes_the_target_and_overrides_to_the_suite(tmp_path):
    suite = write_suite(
        tmp_path,
        """
        import os

        def test_env():
            assert os.environ["CONTRACT_BASE_URL"] == "http://system.test"
            assert os.environ["CONTRACT_ADMIN_KEY"] == "k-admin"
        """,
    )
    run = run_suite(TARGET, overrides={"CONTRACT_ADMIN_KEY": "k-admin"}, suite=suite)
    assert [r["outcome"] for r in run.results] == ["pass"], run.results


def test_assemble_ignores_a_line_cut_short_and_reports_missing_results():
    planned = {"kind": "planned", "test_id": "t.py::a", "endpoints": [], "acs": []}
    run = assemble([planned], aborted=None)
    assert run.aborted == "the suite ended before every planned test reported a result"
    assert run.results == [
        {"test_id": "t.py::a", "outcome": "skip", "message": "not run: the run aborted"}
    ]
    assert assemble([], aborted=None).aborted == "the suite collected no tests"


# --- the HTTP interface ------------------------------------------------------------------------


class Target:
    """A mock system: records requests and answers /test/setup and /test/report."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.setup_response = None  # (status, json) or an exception; None echoes the id
        self.report_response = (
            200,
            {"coverage": {"code": {"covered": 120, "total": 400, "basis": "line coverage"}}},
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/test/setup":
            if isinstance(self.setup_response, Exception):
                raise self.setup_response
            if self.setup_response is not None:
                code, body = self.setup_response
                return httpx.Response(code, json=body) if body is not None else httpx.Response(code)
            sent = json.loads(request.content)
            return httpx.Response(201, json={"session_id": sent["session_id"]})
        if request.url.path == "/test/report":
            if isinstance(self.report_response, Exception):
                raise self.report_response
            code, body = self.report_response
            return httpx.Response(code, json=body)
        return httpx.Response(404)


def scripted_run(*, aborted=None) -> SuiteRun:
    planned = [
        PlannedTest("t.py::a", ["POST /v1/metrics", "GET /v1/events"], ["AC-01"]),
        PlannedTest("t.py::b", ["GET /v1/ledger/verify"], ["AC-06"]),
        PlannedTest("t.py::c", ["POST /v1/webhooks"], ["AC-04"]),
    ]
    results = [
        {"test_id": "t.py::a", "outcome": "pass", "message": "passed"},
        {"test_id": "t.py::b", "outcome": "fail", "message": "assert False"},
        {"test_id": "t.py::c", "outcome": "skip", "message": "no receiver"},
    ]
    return SuiteRun(planned=planned, results=results, aborted=aborted)


@pytest.fixture
def tester():
    target = Target()
    calls: list[tuple[str, dict]] = []
    state = {"run": lambda target_url, overrides: scripted_run()}

    def runner(target_url, overrides):
        calls.append((target_url, overrides))
        return state["run"](target_url, overrides)

    def client():
        with httpx.Client(transport=httpx.MockTransport(target.handle)) as c:
            yield c

    sessions = tester_main.Sessions()
    tester_main.app.dependency_overrides.update(
        {
            tester_main.get_sessions: lambda: sessions,
            tester_main.get_target_client: client,
            tester_main.get_runner: lambda: runner,
        }
    )
    yield TestClient(tester_main.app), target, calls, state
    tester_main.app.dependency_overrides.clear()


def assert_error(resp, status_code: int, source: str) -> None:
    assert resp.status_code == status_code, resp.text
    conforms(resp.json(), "Error")
    assert resp.json()["source"] == source


def setup_session(client) -> str:
    resp = client.post("/test/setup", json={"target_url": TARGET + "/"})
    assert resp.status_code == 201, resp.text
    return resp.json()["session_id"]


def test_setup_creates_a_session_and_sets_up_the_target(tester):
    client, target, *_ = tester
    session_id = setup_session(client)
    uuid.UUID(session_id)
    (request,) = target.requests
    assert str(request.url) == "http://system.test/test/setup"
    assert json.loads(request.content) == {"session_id": session_id, "setup": {}}


@pytest.mark.parametrize(
    "body",
    [
        b"nope",
        b"[]",
        b"{}",
        b'{"target_url": ""}',
        b'{"target_url": "ftp://x"}',
        b'{"target_url": 1}',
    ],
)
def test_invalid_setup_is_400(tester, body):
    client, target, *_ = tester
    assert_error(client.post("/test/setup", content=body), 400, "tester")
    assert target.requests == []


@pytest.mark.parametrize(
    ("answer", "status_code", "source"),
    [
        ((409, {"message": "session_id already exists", "source": "tester"}), 409, "tester"),
        ((500, {"message": "database is down", "source": "system"}), 500, "system"),
        ((400, {"message": "bad request", "source": "tester"}), 400, "tester"),
        ((404, None), 500, "system"),  # target has no testing interface
        ((503, {"detail": "busy"}), 500, "system"),
        ((201, {"session_id": "another-id"}), 500, "system"),
        (httpx.ConnectError("refused"), 500, "communication"),
        (httpx.ReadTimeout("slow"), 500, "communication"),
    ],
)
def test_setup_errors_are_forwarded_and_create_no_session(tester, answer, status_code, source):
    client, target, *_ = tester
    target.setup_response = answer
    resp = client.post("/test/setup", json={"target_url": TARGET})
    assert_error(resp, status_code, source)
    if isinstance(answer, tuple) and answer[1] and "message" in answer[1]:
        assert resp.json()["message"] == answer[1]["message"]


def test_run_returns_and_stores_the_report(tester):
    client, target, calls, _ = tester
    session_id = setup_session(client)
    resp = client.post("/test/run", json={"session_id": session_id})
    assert resp.status_code == 200, resp.text
    report = resp.json()
    conforms(report, "TesterReport")
    assert report_problems(report) == []
    assert (report["session_id"], report["target_url"]) == (session_id, TARGET)
    assert report["aborted"] is None and [r["test_id"] for r in report["results"]] == [
        "t.py::a",
        "t.py::b",
        "t.py::c",
    ]
    coverage = report["coverage"]
    assert coverage["code"] == {"covered": 120, "total": 400, "basis": "line coverage"}
    # Only t.py::a passed: its two endpoints and one AC.
    assert (coverage["component"]["covered"], coverage["component"]["total"]) == (2, len(ENDPOINTS))
    assert (coverage["system"]["covered"], coverage["system"]["total"]) == (1, len(PHASE3_ACS))
    assert calls == [(TARGET, {})]
    report_request = target.requests[-1]
    assert report_request.url.path == "/test/report"
    assert report_request.url.params["session_id"] == session_id

    stored = client.get("/test/report", params={"session_id": session_id})
    assert stored.status_code == 200 and stored.json() == report
    assert client.get("/test/report", params={"session_id": session_id}).json() == report


def test_second_run_is_409_and_runs_nothing(tester):
    client, _, calls, _ = tester
    session_id = setup_session(client)
    assert client.post("/test/run", json={"session_id": session_id}).status_code == 200
    assert_error(client.post("/test/run", json={"session_id": session_id}), 409, "tester")
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"x", b"{}", b'{"session_id": ""}', b'{"session_id": 3}'])
def test_invalid_run_is_400(tester, body):
    client, *_ = tester
    assert_error(client.post("/test/run", content=body), 400, "tester")


def test_unknown_session_is_404(tester):
    client, *_ = tester
    assert_error(client.post("/test/run", json={"session_id": "nope"}), 404, "tester")
    assert_error(client.get("/test/report", params={"session_id": "nope"}), 404, "tester")


def test_report_is_400_without_an_id_and_409_before_the_run(tester):
    client, *_ = tester
    assert_error(client.get("/test/report"), 400, "tester")
    session_id = setup_session(client)
    assert_error(client.get("/test/report", params={"session_id": session_id}), 409, "tester")


def test_aborted_run_is_500_and_its_results_so_far_are_stored(tester):
    client, _, _, state = tester
    state["run"] = lambda target_url, overrides: scripted_run(aborted="exit code 3")
    session_id = setup_session(client)
    assert_error(client.post("/test/run", json={"session_id": session_id}), 500, "tester")
    stored = client.get("/test/report", params={"session_id": session_id})
    assert stored.status_code == 200
    conforms(stored.json(), "TesterReport")
    assert stored.json()["aborted"] == "exit code 3" and len(stored.json()["results"]) == 3


def test_a_crashing_runner_is_500_from_the_tester(tester):
    client, _, _, state = tester

    def crash(target_url, overrides):
        raise RuntimeError("boom")

    state["run"] = crash
    session_id = setup_session(client)
    assert_error(client.post("/test/run", json={"session_id": session_id}), 500, "tester")
    stored = client.get("/test/report", params={"session_id": session_id}).json()
    conforms(stored, "TesterReport")
    assert stored["coverage"]["code"]["covered"] is None


@pytest.mark.parametrize(
    "answer",
    [
        (404, {"message": "unknown session_id", "source": "tester"}),
        (200, {"coverage": {"code": {"covered": 5, "total": 4, "basis": "x"}}}),
        (200, {"coverage": {"code": {"covered": 0, "total": None, "basis": "x"}}}),
        (200, {"coverage": {"code": {"covered": True, "total": 4, "basis": "x"}}}),
        (200, {"coverage": {"code": {"covered": 1, "total": 4, "basis": ""}}}),
        (200, {"no": "coverage"}),
        httpx.ConnectError("refused"),
    ],
)
def test_unusable_target_coverage_is_null_with_the_reason(tester, answer):
    client, target, *_ = tester
    target.report_response = answer
    session_id = setup_session(client)
    report = client.post("/test/run", json={"session_id": session_id}).json()
    code = report["coverage"]["code"]
    assert code["covered"] is None and code["total"] is None
    assert code["basis"].startswith("not measured: ")
    assert report_problems(report) == []


def test_target_null_coverage_is_passed_through(tester):
    client, target, *_ = tester
    target.report_response = (
        200,
        {"coverage": {"code": {"covered": None, "total": None, "basis": "not measured: x"}}},
    )
    session_id = setup_session(client)
    code = client.post("/test/run", json={"session_id": session_id}).json()["coverage"]["code"]
    assert code == {"covered": None, "total": None, "basis": "not measured: x"}


def test_keys_per_target_come_from_the_targets_file(tester, tmp_path, monkeypatch):
    client, _, calls, _ = tester
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({TARGET: {"CONTRACT_ADMIN_KEY": "their-admin"}}))
    monkeypatch.setenv("TESTER_TARGETS_FILE", str(targets))
    session_id = setup_session(client)
    client.post("/test/run", json={"session_id": session_id})
    assert calls == [(TARGET, {"CONTRACT_ADMIN_KEY": "their-admin"})]


def test_document_operations_are_implemented():
    ours = {(m, p) for p, item in tester_main.app.openapi()["paths"].items() for m in item}
    assert DOC["openapi"].startswith("3.1")
    assert {(m, p) for p, item in DOC["paths"].items() for m in item} <= ours
