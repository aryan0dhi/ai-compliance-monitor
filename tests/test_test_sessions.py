"""POST /test/setup and GET /test/report (course cross-team testing interface, D-19)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema_rs import Draft202012Validator

from app.core.config import AppEnv, Settings
from app.db.uow import get_uow
from app.main import create_app
from app.modules.testing import session_router
from app.modules.testing.clock import get_simulated_clock
from app.modules.testing.code_coverage import CodeCoverage, CoverageMeter
from app.modules.testing.sessions import SessionState, TestSessions, get_test_sessions
from tests.fakes import FakeUnitOfWork
from tests.test_testing_routes import FakeSimulatedClock

ROOT = Path(__file__).resolve().parents[1]
DOC = yaml.safe_load(
    (ROOT / "docs" / "testing-interface" / "system-openapi.yaml").read_text(encoding="utf-8")
)


def conforms(body, schema_name: str) -> None:
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": DOC["components"]}
    Draft202012Validator(schema).validate(body)


class FakeMeter:
    def __init__(self) -> None:
        self.restarts = 0
        self.current = CodeCoverage(covered=0, total=100, basis="fake meter")

    def restart(self) -> None:
        self.restarts += 1
        self.current = CodeCoverage(covered=0, total=100, basis="fake meter")

    def snapshot(self) -> CodeCoverage:
        return self.current


@pytest.fixture
def interface(monkeypatch):
    meter, clock, resets = FakeMeter(), FakeSimulatedClock(), []
    sessions = TestSessions(meter)
    monkeypatch.setattr(session_router, "reset_test_environment", resets.append)
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    app.dependency_overrides.update(
        {
            get_test_sessions: lambda: sessions,
            get_simulated_clock: lambda: clock,
            get_uow: lambda: FakeUnitOfWork(),
        }
    )
    return TestClient(app), sessions, meter, clock, resets


def assert_error(resp, status_code: int, source: str) -> None:
    assert resp.status_code == status_code, resp.text
    body = resp.json()
    assert set(body) == {"message", "source"}
    assert body["message"] and body["source"] == source


@pytest.mark.parametrize(
    "settings",
    [
        Settings(enable_test_routes=False),
        Settings(enable_test_routes=True, app_env=AppEnv.PRODUCTION),
    ],
    ids=["disabled", "production"],
)
def test_interface_is_404_unless_test_routes_are_enabled(settings):
    app = create_app(settings)
    client = TestClient(app)
    assert client.post("/test/setup", json={"session_id": "s1", "setup": {}}).status_code == 404
    assert client.get("/test/report", params={"session_id": "s1"}).status_code == 404
    assert not any(p.startswith("/test") for p in app.openapi()["paths"])


def test_setup_resets_restarts_coverage_and_returns_the_id(interface):
    client, sessions, meter, clock, resets = interface
    # No Authorization header: the tester only knows the base URL.
    resp = client.post("/test/setup", json={"session_id": "s1", "setup": {}})
    assert resp.status_code == 201 and resp.json() == {"session_id": "s1"}
    assert len(resets) == 1 and clock.invalidated == 1 and meter.restarts == 1


def test_setup_body_may_omit_setup(interface):
    client, *_ = interface
    assert client.post("/test/setup", json={"session_id": "s1"}).status_code == 201


def test_reused_session_id_is_409(interface):
    client, _, meter, _, resets = interface
    assert client.post("/test/setup", json={"session_id": "s1", "setup": {}}).status_code == 201
    assert_error(client.post("/test/setup", json={"session_id": "s1", "setup": {}}), 409, "tester")
    assert len(resets) == 1 and meter.restarts == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b"{}",
        b'{"session_id": ""}',
        b'{"session_id": "   "}',
        b'{"session_id": 7}',
        json.dumps({"session_id": "x" * 201}).encode(),
        b'{"session_id": "s1", "setup": []}',
        b'{"session_id": "s1", "setup": "baseline"}',
    ],
)
def test_invalid_setup_requests_are_400(interface, body):
    client, _, _, _, resets = interface
    resp = client.post("/test/setup", content=body, headers={"Content-Type": "application/json"})
    assert_error(resp, 400, "tester")
    assert resets == []


def test_failed_preparation_is_500_from_the_system_and_can_be_retried(interface, monkeypatch):
    client, sessions, meter, *_ = interface

    def broken(uow):
        raise ConnectionError("database is down")

    monkeypatch.setattr(session_router, "reset_test_environment", broken)
    assert_error(client.post("/test/setup", json={"session_id": "s1"}), 500, "system")
    assert meter.restarts == 0
    assert_error(client.get("/test/report", params={"session_id": "s1"}), 404, "tester")

    monkeypatch.setattr(session_router, "reset_test_environment", lambda uow: None)
    assert client.post("/test/setup", json={"session_id": "s1"}).status_code == 201


def test_report_returns_code_coverage_and_reading_it_changes_nothing(interface):
    client, _, meter, *_ = interface
    client.post("/test/setup", json={"session_id": "s1", "setup": {}})
    meter.current = CodeCoverage(covered=40, total=100, basis="fake meter")
    first = client.get("/test/report", params={"session_id": "s1"})
    assert first.status_code == 200
    assert first.json() == {
        "session_id": "s1",
        "coverage": {"code": {"covered": 40, "total": 100, "basis": "fake meter"}},
    }
    assert client.get("/test/report", params={"session_id": "s1"}).json() == first.json()


def test_unmeasured_coverage_is_null_never_zero(interface):
    client, _, meter, *_ = interface
    client.post("/test/setup", json={"session_id": "s1"})
    meter.current = CodeCoverage(covered=None, total=None, basis="not measured: no coverage.py")
    code = client.get("/test/report", params={"session_id": "s1"}).json()["coverage"]["code"]
    assert code == {"covered": None, "total": None, "basis": "not measured: no coverage.py"}


@pytest.mark.parametrize("params", [{}, {"session_id": ""}])
def test_report_without_session_id_is_400(interface, params):
    client, *_ = interface
    assert_error(client.get("/test/report", params=params), 400, "tester")


def test_report_for_unknown_session_is_404(interface):
    client, *_ = interface
    assert_error(client.get("/test/report", params={"session_id": "nope"}), 404, "tester")


def test_report_and_second_setup_are_409_while_setup_is_in_progress(interface):
    client, sessions, *_ = interface
    sessions.begin("s1", {})  # setup started but not finished
    assert_error(client.get("/test/report", params={"session_id": "s1"}), 409, "tester")
    assert_error(client.post("/test/setup", json={"session_id": "s2"}), 409, "tester")


def test_new_setup_ends_the_active_session_and_freezes_its_report(interface):
    client, _, meter, *_ = interface
    client.post("/test/setup", json={"session_id": "s1"})
    meter.current = CodeCoverage(covered=40, total=100, basis="fake meter")
    client.post("/test/setup", json={"session_id": "s2"})
    meter.current = CodeCoverage(covered=7, total=100, basis="fake meter")

    old = client.get("/test/report", params={"session_id": "s1"}).json()["coverage"]["code"]
    assert (old["covered"], old["total"]) == (40, 100)
    assert "frozen when session 's2' began" in old["basis"]
    new = client.get("/test/report", params={"session_id": "s2"}).json()["coverage"]["code"]
    assert (new["covered"], new["total"], new["basis"]) == (7, 100, "fake meter")


def test_document_operations_are_implemented():
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    ours = {(m, p) for p, item in app.openapi()["paths"].items() for m in item}
    assert DOC["openapi"].startswith("3.1")
    assert {(m, p) for p, item in DOC["paths"].items() for m in item} <= ours


def test_responses_conform_to_the_document(interface):
    client, _, meter, *_ = interface
    conforms(client.post("/test/setup", json={"session_id": "s1"}).json(), "SessionCreated")
    for code in (
        CodeCoverage(covered=40, total=100, basis="fake meter"),
        CodeCoverage(covered=None, total=None, basis="not measured: x"),
    ):
        meter.current = code
        conforms(client.get("/test/report", params={"session_id": "s1"}).json(), "SystemReport")
    for resp in (
        client.post("/test/setup", json={"session_id": "s1"}),
        client.post("/test/setup", content=b"[]"),
        client.get("/test/report"),
        client.get("/test/report", params={"session_id": "nope"}),
    ):
        conforms(resp.json(), "Error")


class TestSessionRegistry:
    def test_oldest_sessions_are_forgotten_beyond_the_limit(self):
        sessions = TestSessions(FakeMeter(), max_sessions=2)
        for sid in ("a", "b", "c"):
            sessions.begin(sid, {})
            sessions.activate(sid)
        assert [s.session_id for s in sessions._sessions.values()] == ["b", "c"]
        assert sessions._sessions["b"].state is SessionState.ENDED
        assert sessions._sessions["c"].state is SessionState.ACTIVE


class TestCoverageMeter:
    def test_before_any_session_nothing_is_measured(self):
        code = CoverageMeter().snapshot()
        assert (code.covered, code.total) == (None, None) and code.basis.startswith("not measured")

    def test_without_coverage_installed_it_reports_null_with_the_reason(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "coverage", None)  # import coverage -> ImportError
        meter = CoverageMeter()
        meter.restart()
        code = meter.snapshot()
        assert (code.covered, code.total) == (None, None)
        assert "INSTALL_COVERAGE" in code.basis

    def test_real_measurement_in_a_clean_process(self):
        # A subprocess, so this measurement and pytest-cov's never share a process.
        env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE", "COVERAGE"))}
        out = subprocess.run(
            [sys.executable, "-m", "tests.coverage_meter_probe"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
        before, after, again, stopped = (result[k] for k in ("before", "after", "again", "stopped"))
        assert before["basis"] == after["basis"] and "API process" in after["basis"]
        assert 0 <= before["covered"] < after["covered"] < after["total"] == before["total"]
        assert again == after  # reading a snapshot doesn't change it
        assert stopped["covered"] is None and stopped["total"] is None
        outer = result["outer"]
        assert outer["covered"] is None and "outer coverage run" in outer["basis"]
