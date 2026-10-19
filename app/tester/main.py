"""Hosted cross-team tester, served at <site>/tester (registry "Tester base URL").

Runs the black-box contract suite (tests/contract) against one allowlisted pod service and serves
its report (results plus REQ, AC, endpoint, Model 1 and Model 4 coverage):

    POST /runs              start a run: {target_url, system_key, viewer_key, admin_key, ...}
    GET  /runs/{run_id}     status, summary, coverage and per-test results (JSON)
    GET  /runs/{run_id}/report   the same report as Markdown
    GET  /runs              recent runs

Safety: only targets in TESTER_ALLOWED_TARGETS are accepted, one run executes at a time, each run
is capped at TESTER_RUN_TIMEOUT_S, and the target's keys go only to that run's test process: they
are never stored, logged or returned. While a run is active, the suite's webhook receiver listens
on TESTER_RECEIVER_BIND and is reachable publicly at TESTER_PUBLIC_URL/hook/<run_id>.
"""

import json
import os
import subprocess  # nosec B404 - runs pytest with a fixed argument list, never a shell
import sys
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "tests" / "contract"
MAX_KEPT_RUNS = 50


def origin(url: str) -> str | None:
    """scheme://host[:port], lower-cased, default ports dropped; None if not an http(s) URL."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    default = 443 if parts.scheme == "https" else 80
    host = parts.hostname.lower()
    return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")


class RunIn(BaseModel):
    target_url: str = Field(max_length=500, description="Base URL of an allowlisted pod service")
    system_key: str = Field(min_length=1, max_length=500, description="Monitored-system key")
    viewer_key: str = Field(min_length=1, max_length=500)
    admin_key: str = Field(min_length=1, max_length=500, description="Key for reset/clock/webhooks")
    system_id: str | None = Field(None, max_length=100, description="Default: the pod fixture id")
    drift_metric: str | None = Field(None, max_length=100)
    drift_limit: float | None = None
    perf_metric: str | None = Field(None, max_length=100)
    perf_limit: float | None = None
    sampling_interval_s: int | None = Field(None, ge=1, le=86400)
    webhooks: bool = Field(True, description="Check webhook delivery via this tester's receiver")


@dataclass
class Run:
    run_id: str
    target_url: str
    created_at: str
    status: str = "running"  # running | finished | timeout | error
    finished_at: str | None = None
    exit_code: int | None = None
    report: dict[str, Any] | None = None
    report_md: str | None = None
    log_tail: str = ""

    def public(self, *, full: bool) -> dict[str, Any]:
        out: dict[str, Any] = {
            "run_id": self.run_id,
            "target_url": self.target_url,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "summary": (self.report or {}).get("summary"),
        }
        if full:
            out.update(
                exit_code=self.exit_code,
                coverage=(self.report or {}).get("coverage"),
                tests=(self.report or {}).get("tests"),
                log_tail=self.log_tail,
            )
        return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def run_suite(
    env: dict[str, str], timeout_s: int
) -> tuple[str, int | None, dict | None, str | None, str]:
    """Run tests/contract in a child process. Returns (status, exit code, report, markdown, log)."""
    with tempfile.TemporaryDirectory() as out_dir:
        env = {**env, "CONTRACT_REPORT_DIR": out_dir}
        command = [
            sys.executable,
            "-m",
            "pytest",
            str(SUITE),
            "-m",
            "contract",
            "-q",
            "-p",
            "no:cacheprovider",
        ]
        try:
            done = subprocess.run(  # nosec B603 - fixed argv, no shell; inputs only reach env vars
                command, env=env, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return "timeout", None, None, None, log[-4000:]
        report_path, md_path = (
            Path(out_dir, "contract-report.json"),
            Path(out_dir, "contract-report.md"),
        )
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        markdown = md_path.read_text() if md_path.exists() else None
        return (
            ("finished" if report else "error"),
            done.returncode,
            report,
            markdown,
            done.stdout[-4000:],
        )


Runner = Callable[[dict[str, str], int], tuple[str, int | None, dict | None, str | None, str]]


@dataclass
class Tester:
    allowed: set[str]
    public_url: str | None
    receiver_bind: str = "0.0.0.0:8099"  # container-internal; Caddy proxies /tester/hook here
    timeout_s: int = 600
    runner: Runner = run_suite
    runs: dict[str, Run] = field(default_factory=dict)
    busy: threading.Lock = field(default_factory=threading.Lock)

    def environment(self, body: RunIn, run_id: str) -> dict[str, str]:
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", tempfile.gettempdir()),
            "CONTRACT_BASE_URL": body.target_url.rstrip("/"),
            "CONTRACT_SYSTEM_KEY": body.system_key,
            "CONTRACT_VIEWER_KEY": body.viewer_key,
            "CONTRACT_ADMIN_KEY": body.admin_key,
        }
        optional = {
            "CONTRACT_SYSTEM_ID": body.system_id,
            "CONTRACT_DRIFT_METRIC": body.drift_metric,
            "CONTRACT_DRIFT_LIMIT": body.drift_limit,
            "CONTRACT_PERF_METRIC": body.perf_metric,
            "CONTRACT_PERF_LIMIT": body.perf_limit,
            "CONTRACT_SAMPLING_INTERVAL_S": body.sampling_interval_s,
        }
        env.update({k: str(v) for k, v in optional.items() if v is not None})
        if body.webhooks and self.public_url:
            env["CONTRACT_RECEIVER_BIND"] = self.receiver_bind
            env["CONTRACT_RECEIVER_URL"] = f"{self.public_url.rstrip('/')}/hook/{run_id}"
        else:
            env["CONTRACT_WEBHOOKS"] = "off"
        return env

    def start(self, body: RunIn) -> Run:
        target = origin(body.target_url)
        if target is None or target not in self.allowed:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"target_url must be one of the allowlisted services: {sorted(self.allowed)}",
            )
        if not self.busy.acquire(blocking=False):
            raise HTTPException(status.HTTP_409_CONFLICT, "a run is in progress; one at a time")
        run = Run(run_id=uuid4().hex[:12], target_url=target, created_at=_now())
        env = self.environment(body, run.run_id)
        self.runs[run.run_id] = run
        while len(self.runs) > MAX_KEPT_RUNS:
            self.runs.pop(next(iter(self.runs)))

        def execute() -> None:
            try:
                run.status, run.exit_code, run.report, run.report_md, run.log_tail = self.runner(
                    env, self.timeout_s
                )
            except Exception as exc:  # report the failure instead of losing the run
                run.status, run.log_tail = "error", f"{type(exc).__name__}: {exc}"
            finally:
                run.finished_at = _now()
                self.busy.release()

        threading.Thread(target=execute, name=f"tester-{run.run_id}", daemon=True).start()
        return run


def create_tester_app(tester: Tester) -> FastAPI:
    app = FastAPI(
        title="AI Compliance Monitor - cross-team tester",
        version="1.0.0",
        description=__doc__,
    )

    @app.get("/")
    def index() -> dict[str, Any]:
        return {
            "service": "cross-team contract tester (tests/contract)",
            "start_a_run": "POST /tester/runs with target_url, system_key, viewer_key, admin_key",
            "allowed_targets": sorted(tester.allowed),
            "max_run_seconds": tester.timeout_s,
            "one_run_at_a_time": True,
            "running": [r.run_id for r in tester.runs.values() if r.status == "running"],
            "docs": "/tester/docs",
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", status_code=status.HTTP_202_ACCEPTED)
    def start_run(body: RunIn) -> dict[str, Any]:
        run = tester.start(body)
        return {**run.public(full=False), "status_url": f"/tester/runs/{run.run_id}"}

    @app.get("/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [r.public(full=False) for r in reversed(tester.runs.values())]

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = tester.runs.get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run")
        return run.public(full=True)

    @app.get("/runs/{run_id}/report", response_class=PlainTextResponse)
    def get_report(run_id: str) -> str:
        run = tester.runs.get(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown run")
        if run.report_md is None:
            raise HTTPException(status.HTTP_409_CONFLICT, f"no report yet (status: {run.status})")
        return run.report_md

    return app


def tester_from_env() -> Tester:
    allowed = {
        o for o in (origin(u) for u in os.environ.get("TESTER_ALLOWED_TARGETS", "").split(",")) if o
    }
    return Tester(
        allowed=allowed,
        public_url=os.environ.get("TESTER_PUBLIC_URL") or None,
        receiver_bind=os.environ.get("TESTER_RECEIVER_BIND", "0.0.0.0:8099"),
        timeout_s=int(os.environ.get("TESTER_RUN_TIMEOUT_S", "600")),
    )


app = create_tester_app(tester_from_env())
