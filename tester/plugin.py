"""pytest plugin loaded by the tester's suite subprocess (-p tester.plugin).

Appends one JSON object per line to $TESTER_RESULTS_FILE as the run goes, so the results so far
survive an abort:

    {"kind": "planned", "test_id", "endpoints", "acs"}   every collected test, in run order
    {"kind": "started", "test_id"}
    {"kind": "result", "test_id", "outcome", "message", "source"?}

Outcomes are the interface's pass / fail / error / skip. A failed assertion is "fail". Any other
exception is "error", with its source: the exception's `error_source` attribute if it has one
(the contract suite's TargetServerError says "system"), "communication" for timeouts and
transport errors, otherwise "tester".
"""

import json
import os
from typing import Any

import httpx
import pytest

_results_file = os.environ.get("TESTER_RESULTS_FILE")
_phases: dict[str, dict[str, Any]] = {}

MAX_MESSAGE = 500


def _emit(record: dict[str, Any]) -> None:
    if _results_file is None:
        return
    with open(_results_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _test_id(nodeid: str) -> str:
    """Unique within a run: file name plus the rest of the node id ("test_x.py::test_y[id]").

    Only the file part is shortened; parametrize ids may contain "/" themselves.
    """
    path, _, rest = nodeid.partition("::")
    return f"{path.rsplit('/', 1)[-1]}::{rest}" if rest else path


def _tags(item: pytest.Item, key: str) -> list[str]:
    marker = item.get_closest_marker("covers")
    return list(marker.kwargs.get(key, [])) if marker else []


def pytest_collection_finish(session: pytest.Session) -> None:
    for item in session.items:
        _emit(
            {
                "kind": "planned",
                "test_id": _test_id(item.nodeid),
                "endpoints": _tags(item, "endpoints"),
                "acs": _tags(item, "acs"),
            }
        )


def pytest_runtest_logstart(nodeid: str, location: Any) -> None:
    _emit({"kind": "started", "test_id": _test_id(nodeid)})


def _classify(excinfo: pytest.ExceptionInfo[BaseException]) -> tuple[str, str | None]:
    exc = excinfo.value
    if isinstance(exc, AssertionError) or excinfo.errisinstance(pytest.fail.Exception):
        return "fail", None
    source = getattr(exc, "error_source", None)
    if source in ("tester", "system", "communication", "unknown"):
        return "error", source
    if isinstance(exc, httpx.TimeoutException | httpx.TransportError):
        return "error", "communication"
    return "error", "tester"


def _message(report: pytest.TestReport, fallback: str) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    text = getattr(crash, "message", None) or str(report.longrepr or "") or fallback
    return text.strip()[:MAX_MESSAGE] or fallback


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Any:
    outcome = yield
    report: pytest.TestReport = outcome.get_result()
    phases = _phases.setdefault(report.nodeid, {})
    if report.skipped:
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else "skipped"
        phases.setdefault("result", ("skip", str(reason).removeprefix("Skipped: "), None))
    elif report.failed and call.excinfo is not None:
        kind, source = _classify(call.excinfo)
        if report.when != "call":  # a fixture or teardown broke: the test itself didn't fail
            kind, source = "error", source or "tester"
        phases.setdefault("result", (kind, _message(report, f"{kind} in {report.when}"), source))


def pytest_runtest_logfinish(nodeid: str, location: Any) -> None:
    kind, message, source = _phases.get(nodeid, {}).get("result", ("pass", "passed", None))
    record = {
        "kind": "result",
        "test_id": _test_id(nodeid),
        "outcome": kind,
        "message": message,
    }
    if source is not None:
        record["source"] = source
    _emit(record)
