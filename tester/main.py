"""Tester service of the course cross-team testing interface
(docs/testing-interface/tester-openapi.yaml, docs/cross-team-testing.md section 2a).

    POST /test/setup  {"target_url"}   new session; sets up the target's POST /test/setup
    POST /test/run    {"session_id"}   runs the contract suite (tests/contract) against it
    GET  /test/report ?session_id=     the stored report, unchanged

A separate app from the system (uvicorn tester.main:app). Sessions and reports are kept in
memory until the tester stops. The target's /v1 keys come from this process's CONTRACT_*
environment, or per target from the JSON file named by TESTER_TARGETS_FILE:
{"<target_url>": {"CONTRACT_SYSTEM_KEY": "...", ...}}.
"""

import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from tester.runner import SuiteRun, run_suite
from tests.contract.scope import ENDPOINTS, PHASE3_ACS

logger = logging.getLogger("tester")

SETUP_TIMEOUT_S = float(os.environ.get("TESTER_SETUP_TIMEOUT_S", "120"))
REPORT_TIMEOUT_S = float(os.environ.get("TESTER_REPORT_TIMEOUT_S", "30"))
RUN_TIMEOUT_S = float(os.environ.get("TESTER_RUN_TIMEOUT_S", "1800"))
SOURCES = ("tester", "system", "communication", "unknown")
NOT_MEASURED_TESTER_FAILED = {
    "covered": None,
    "total": None,
    "basis": "not measured: the tester failed",
}


class State(StrEnum):
    READY = "ready"
    RUNNING = "running"
    DONE = "done"


@dataclass
class Session:
    session_id: str
    target_url: str
    state: State = State.READY
    report: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class Sessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_id: dict[str, Session] = {}

    def add(self, session: Session) -> None:
        with self._lock:
            self._by_id[session.session_id] = session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._by_id.get(session_id)


_sessions = Sessions()


def get_sessions() -> Sessions:
    return _sessions


def get_target_client() -> Iterator[httpx.Client]:
    with httpx.Client() as client:
        yield client


Runner = Callable[[str, dict[str, str]], SuiteRun]


def get_runner() -> Runner:
    return lambda target, overrides: run_suite(target, overrides=overrides, timeout_s=RUN_TIMEOUT_S)


def target_overrides(target_url: str) -> dict[str, str]:
    path = os.environ.get("TESTER_TARGETS_FILE")
    if not path:
        return {}
    targets = json.loads(Path(path).read_text(encoding="utf-8"))
    return {k: str(v) for k, v in targets.get(target_url, {}).items()}


def error(status_code: int, message: str, source: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"message": message, "source": source})


async def _json_object(request: Request) -> dict[str, Any] | None:
    try:
        body = json.loads(await request.body())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _target_url(value: Any) -> str | None:
    if not _non_empty_str(value):
        return None
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value.strip().rstrip("/")


def _forwarded_error(resp: httpx.Response) -> JSONResponse:
    """A target error, passed on with its status, message and source when it is well formed."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if (
        isinstance(body, dict)
        and _non_empty_str(body.get("message"))
        and body.get("source") in SOURCES
        and resp.status_code in (400, 404, 409, 500)
    ):
        return error(resp.status_code, body["message"], body["source"])
    return error(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        f"the target's /test/setup answered {resp.status_code} without a valid error body",
        "system",
    )


def _code_coverage(client: httpx.Client, session: Session) -> dict[str, Any]:
    def unavailable(why: str) -> dict[str, Any]:
        return {"covered": None, "total": None, "basis": f"not measured: {why}"}

    try:
        resp = client.get(
            f"{session.target_url}/test/report",
            params={"session_id": session.session_id},
            timeout=REPORT_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        return unavailable(f"the target's /test/report was unreachable ({type(exc).__name__})")
    if resp.status_code != 200:
        return unavailable(f"the target's /test/report answered {resp.status_code}")
    try:
        code = resp.json()["coverage"]["code"]
        covered, total, basis = code["covered"], code["total"], code["basis"]
    except (ValueError, KeyError, TypeError):
        return unavailable("the target's /test/report had no coverage.code")
    numbers_ok = (covered is None and total is None) or (
        isinstance(covered, int)
        and isinstance(total, int)
        and not isinstance(covered, bool)
        and not isinstance(total, bool)
        and 0 <= covered <= total
    )
    if not numbers_ok or not _non_empty_str(basis):
        return unavailable("the target's coverage.code was malformed")
    return {"covered": covered, "total": total, "basis": basis}


def _scope_coverage(run: SuiteRun, key: str, universe: list[str], what: str) -> dict[str, Any]:
    outcomes = {r["test_id"]: r["outcome"] for r in run.results}
    covered = {
        tag
        for test in run.planned
        if outcomes.get(test.test_id) == "pass"
        for tag in getattr(test, key)
        if tag in universe
    }
    return {
        "covered": len(covered),
        "total": len(universe),
        "basis": f"{what} with at least one passing test (tags from the suite's @covers)",
    }


def build_report(session: Session, run: SuiteRun, code: dict[str, Any]) -> dict[str, Any]:
    return {
        "session_id": session.session_id,
        "target_url": session.target_url,
        "finished_at": datetime.now(UTC).isoformat(),
        "aborted": run.aborted,
        "results": run.results,
        "coverage": {
            "code": code,
            "component": _scope_coverage(
                run, "endpoints", ENDPOINTS, "Phase 3 pod-interface endpoints (Section 5)"
            ),
            "system": _scope_coverage(
                run, "acs", PHASE3_ACS, "Phase 3 acceptance criteria of the pod specification"
            ),
        },
    }


app = FastAPI(
    title="AI Compliance Monitor - tester",
    version="1.0.0",
    description="Runs the pod contract suite against a target system (course testing interface).",
)


@app.post("/test/setup", status_code=status.HTTP_201_CREATED)
async def setup(
    request: Request,
    sessions: Sessions = Depends(get_sessions),
    client: httpx.Client = Depends(get_target_client),
) -> JSONResponse:
    body = await _json_object(request)
    if body is None:
        return error(status.HTTP_400_BAD_REQUEST, "body must be a JSON object", "tester")
    target_url = _target_url(body.get("target_url"))
    if target_url is None:
        return error(status.HTTP_400_BAD_REQUEST, "target_url must be an http(s) URL", "tester")
    session = Session(session_id=str(uuid.uuid4()), target_url=target_url)
    try:
        resp = await run_in_threadpool(
            client.post,
            f"{target_url}/test/setup",
            json={"session_id": session.session_id, "setup": {}},
            timeout=SETUP_TIMEOUT_S,
        )
    except httpx.HTTPError as exc:
        return error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"could not reach the target's /test/setup ({type(exc).__name__})",
            "communication",
        )
    except Exception:
        logger.exception("tester setup failed")
        return error(status.HTTP_500_INTERNAL_SERVER_ERROR, "the tester failed", "tester")
    if resp.status_code != 201:
        return _forwarded_error(resp)
    try:
        echoed = resp.json().get("session_id")
    except (ValueError, AttributeError):
        echoed = None
    if echoed != session.session_id:
        return error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "the target's /test/setup did not return the session_id it was given",
            "system",
        )
    sessions.add(session)
    return JSONResponse(
        status_code=status.HTTP_201_CREATED, content={"session_id": session.session_id}
    )


@app.post("/test/run")
async def run(
    request: Request,
    sessions: Sessions = Depends(get_sessions),
    client: httpx.Client = Depends(get_target_client),
    runner: Runner = Depends(get_runner),
) -> JSONResponse:
    body = await _json_object(request)
    if body is None or not _non_empty_str(body.get("session_id")):
        return error(status.HTTP_400_BAD_REQUEST, "session_id must be a non-empty string", "tester")
    session = sessions.get(body["session_id"])
    if session is None:
        return error(status.HTTP_404_NOT_FOUND, "unknown session_id", "tester")
    with session.lock:
        if session.state is not State.READY:
            return error(status.HTTP_409_CONFLICT, "this session was already run", "tester")
        session.state = State.RUNNING
    try:
        overrides = target_overrides(session.target_url)
        suite = await run_in_threadpool(runner, session.target_url, overrides)
        code = await run_in_threadpool(_code_coverage, client, session)
        report = build_report(session, suite, code)
    except Exception:
        logger.exception("tester run failed")
        suite = SuiteRun(planned=[], results=[], aborted="the tester failed")
        report = build_report(session, suite, NOT_MEASURED_TESTER_FAILED)
    session.report, session.state = report, State.DONE
    if report["aborted"]:
        return error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"the run aborted: {report['aborted']}; GET /test/report has the results so far",
            "tester",
        )
    return JSONResponse(content=report)


@app.get("/test/report")
def report(
    session_id: str | None = None, sessions: Sessions = Depends(get_sessions)
) -> JSONResponse:
    if not session_id:
        return error(status.HTTP_400_BAD_REQUEST, "the session_id query is required", "tester")
    session = sessions.get(session_id)
    if session is None:
        return error(status.HTTP_404_NOT_FOUND, "unknown session_id", "tester")
    if session.report is None:
        return error(
            status.HTTP_409_CONFLICT, "the suite hasn't finished for this session", "tester"
        )
    return JSONResponse(content=session.report)
