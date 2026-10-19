"""Black-box contract suite for cross-team testing (Phase 3 Q6.1; Section 5 common interface).

Runs over HTTP against ANY service implementing the pod's /v1 contract: ours (local or deployed)
or a podmate's. Nothing here imports the application. Configure with environment variables:

    CONTRACT_BASE_URL       service root, e.g. https://monitor.example.com     (required)
    CONTRACT_SYSTEM_KEY     monitored-system key for CONTRACT_SYSTEM_ID         (required)
    CONTRACT_VIEWER_KEY     viewer key                                           (required)
    CONTRACT_ADMIN_KEY      admin key (webhooks, /v1/test/*)                     (required)
    CONTRACT_SYSTEM_ID      default 550e8400-e29b-41d4-a716-446655440000 (Section 5.2 example)
    CONTRACT_DRIFT_METRIC / CONTRACT_DRIFT_LIMIT     default drift_score / 0.30, breaches above
    CONTRACT_PERF_METRIC / CONTRACT_PERF_LIMIT       default accuracy / 0.70, breaches below
    CONTRACT_SAMPLING_INTERVAL_S                     default 300
    CONTRACT_RECEIVER_URL   public URL the service can POST webhooks to, served by this run
                            (with CONTRACT_RECEIVER_BIND host:port). Default: a local receiver
                            on 127.0.0.1, which only a service on the same machine can reach.
    CONTRACT_WEBHOOKS=off   skip the webhook checks (no reachable receiver)
    CONTRACT_WAIT_S         how long to wait for asynchronous results (default 60)
    CONTRACT_REPORT_DIR     where contract-report.json / .md are written (default: cwd)

Run: pytest tests/contract -m contract -v
Each test declares what it covers (@pytest.mark.covers); the report aggregates component and
system-model coverage, and marks failures that depend on an open pod decision (D-xx).
"""

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.contract.scope import ENDPOINTS, MODEL1, MODEL4, PHASE3_ACS, PHASE3_REQS


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "covers(reqs, acs, endpoints, model1, model4, decision): what a contract test exercises",
    )


def pytest_collection_modifyitems(config, items):
    enabled = bool(os.environ.get("CONTRACT_BASE_URL"))
    skip = pytest.mark.skip(reason="set CONTRACT_BASE_URL (and keys) to run the contract suite")
    for item in items:
        if "contract" in item.path.parts:
            item.add_marker(pytest.mark.contract)
            if not enabled:
                item.add_marker(skip)


# --- configuration ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    base_url: str
    keys: dict[str, str]
    system_id: str
    drift_metric: str
    drift_limit: float
    perf_metric: str
    perf_limit: float
    interval_s: int
    wait_s: float


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        pytest.fail(f"{name} is required for the contract suite")
    return value


@pytest.fixture(scope="session")
def target() -> Target:
    return Target(
        base_url=_env("CONTRACT_BASE_URL").rstrip("/"),
        keys={
            "system": _env("CONTRACT_SYSTEM_KEY"),
            "viewer": _env("CONTRACT_VIEWER_KEY"),
            "admin": _env("CONTRACT_ADMIN_KEY"),
        },
        system_id=_env("CONTRACT_SYSTEM_ID", "550e8400-e29b-41d4-a716-446655440000"),
        drift_metric=_env("CONTRACT_DRIFT_METRIC", "drift_score"),
        drift_limit=float(_env("CONTRACT_DRIFT_LIMIT", "0.30")),
        perf_metric=_env("CONTRACT_PERF_METRIC", "accuracy"),
        perf_limit=float(_env("CONTRACT_PERF_LIMIT", "0.70")),
        interval_s=int(_env("CONTRACT_SAMPLING_INTERVAL_S", "300")),
        wait_s=float(_env("CONTRACT_WAIT_S", "60")),
    )


class TargetServerError(Exception):
    """The service answered 5xx. No check expects one, so it is an error of the system under
    test, not a failed assertion (the tester service reports it with source "system")."""

    error_source = "system"


class Api:
    def __init__(self, target: Target) -> None:
        self.target = target
        self.http = httpx.Client(base_url=target.base_url, timeout=30)

    def call(self, method: str, path: str, *, as_: str | None, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        if as_ is not None:
            headers["Authorization"] = f"Bearer {self.target.keys[as_]}"
        resp = self.http.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 500:
            raise TargetServerError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp

    def wait_for(self, check: Callable[[], Any], what: str) -> Any:
        deadline = time.monotonic() + self.target.wait_s
        while time.monotonic() < deadline:
            result = check()
            if result:
                return result
            time.sleep(1)
        pytest.fail(f"timed out after {self.target.wait_s}s waiting for {what}")


@pytest.fixture(scope="session")
def api(target) -> Iterator[Api]:
    client = Api(target)
    yield client
    client.http.close()


@pytest.fixture(scope="session")
def test_routes(api) -> bool:
    """Reset the service once; True if it offers the test-only routes (clock, reset)."""
    resp = api.call("POST", "/v1/test/reset", as_="admin")
    return resp.status_code == 204


# --- webhook receiver ---------------------------------------------------------------------------


@dataclass
class Receiver:
    url: str | None
    requests: list[dict[str, Any]] = field(default_factory=list)

    def bodies(self) -> list[dict[str, Any]]:
        out = []
        for r in list(self.requests):
            try:
                out.append(json.loads(r["body"]))
            except ValueError:
                pass
        return out


@pytest.fixture(scope="session")
def receiver(api, test_routes) -> Iterator[Receiver]:
    if os.environ.get("CONTRACT_WEBHOOKS") == "off":
        yield Receiver(url=None)
        return
    bind = os.environ.get("CONTRACT_RECEIVER_BIND", "127.0.0.1:0")
    host, port = bind.rsplit(":", 1)
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            received.append({"at": datetime.now(UTC), "headers": dict(self.headers), "body": body})
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer((host, int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = os.environ.get("CONTRACT_RECEIVER_URL") or (
        f"http://127.0.0.1:{server.server_address[1]}/contract-hook"
    )
    resp = api.call(
        "POST",
        "/v1/webhooks",
        as_="admin",
        json={"url": url, "event_types": ["flag_notification", "reminder"]},
    )
    if resp.status_code != 201:
        server.shutdown()
        pytest.fail(f"webhook registration failed: {resp.status_code} {resp.text[:300]}")
    rec = Receiver(url=url, requests=received)
    yield rec
    server.shutdown()


# --- coverage report ----------------------------------------------------------------------------

_RESULTS: list[dict[str, Any]] = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("covers")
    if marker is None or "contract" not in item.path.parts:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        tags = {k: list(v) if isinstance(v, list | tuple) else v for k, v in marker.kwargs.items()}
        _RESULTS.append(
            {
                "test": item.name,
                "outcome": report.outcome,
                "detail": str(report.longrepr)[-600:] if report.failed else None,
                **tags,
            }
        )


def _coverage(key: str, universe: list[str]) -> dict[str, Any]:
    passed = {x for r in _RESULTS if r["outcome"] == "passed" for x in r.get(key, [])}
    attempted = {x for r in _RESULTS for x in r.get(key, [])}
    return {
        "covered": sorted(passed & set(universe)),
        "attempted_not_passed": sorted((attempted - passed) & set(universe)),
        "not_exercised": sorted(set(universe) - attempted),
        "ratio": f"{len(passed & set(universe))}/{len(universe)}",
    }


def pytest_sessionfinish(session, exitstatus):
    if not _RESULTS:
        return
    report = {
        "target": os.environ.get("CONTRACT_BASE_URL"),
        "finished_at": datetime.now(UTC).isoformat(),
        "summary": {
            o: sum(r["outcome"] == o for r in _RESULTS) for o in ("passed", "failed", "skipped")
        },
        "coverage": {
            "requirements": _coverage("reqs", PHASE3_REQS),
            "acceptance_criteria": _coverage("acs", PHASE3_ACS),
            "endpoints (component)": _coverage("endpoints", ENDPOINTS),
            "model1_states (system model)": _coverage("model1", MODEL1),
            "model4_invariants (system model)": _coverage("model4", MODEL4),
        },
        "tests": _RESULTS,
    }
    out_dir = Path(os.environ.get("CONTRACT_REPORT_DIR", "."))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "contract-report.json").write_text(json.dumps(report, indent=2, default=str))
    lines = [
        f"# Contract run against {report['target']}",
        "",
        f"Finished {report['finished_at']}: "
        + ", ".join(f"{n} {o}" for o, n in report["summary"].items()),
        "",
        "| Coverage | Ratio | Not passed | Not exercised |",
        "|---|---|---|---|",
    ]
    for name, c in report["coverage"].items():
        lines.append(
            f"| {name} | {c['ratio']} | {', '.join(c['attempted_not_passed']) or '-'} | "
            f"{', '.join(c['not_exercised']) or '-'} |"
        )
    lines += ["", "| Test | Outcome | Covers | Open decision |", "|---|---|---|---|"]
    for r in _RESULTS:
        covers = ", ".join(r.get("reqs", []) + r.get("acs", []))
        lines.append(f"| {r['test']} | {r['outcome']} | {covers} | {r.get('decision') or '-'} |")
    failed = [r for r in _RESULTS if r["outcome"] == "failed"]
    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            note = f" (depends on open decision {r['decision']})" if r.get("decision") else ""
            lines += [f"### {r['test']}{note}", "", "```", (r["detail"] or "").strip(), "```", ""]
    (out_dir / "contract-report.md").write_text("\n".join(lines) + "\n")
