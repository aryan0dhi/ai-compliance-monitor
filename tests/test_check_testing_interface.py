"""The CI self-test's report and error-body rules (scripts/check_testing_interface.py)."""

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_testing_interface.py"
_spec = importlib.util.spec_from_file_location("check_testing_interface", SCRIPT)
assert _spec is not None and _spec.loader is not None
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

GOOD: dict[str, Any] = {
    "session_id": "s1",
    "target_url": "http://api:8000",
    "results": [
        {"test_id": "t.py::a", "outcome": "pass", "message": "passed"},
        {"test_id": "t.py::b", "outcome": "error", "message": "503", "source": "system"},
        {"test_id": "t.py::c", "outcome": "skip", "message": "not run"},
    ],
    "coverage": {
        "code": {"covered": 10, "total": 20, "basis": "lines"},
        "component": {"covered": 9, "total": 9, "basis": "endpoints"},
        "system": {"covered": None, "total": None, "basis": "not measured: x"},
    },
}


def broken(path: list, value):
    report = copy.deepcopy(GOOD)
    node: Any = report
    for key in path[:-1]:
        node = node[key]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return report


def test_a_well_formed_report_has_no_problems():
    assert check.report_problems(GOOD, "s1") == []


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (["session_id"], "other"),
        (["results"], []),
        (["results", 1, "test_id"], "t.py::a"),  # duplicate
        (["results", 0, "test_id"], ""),
        (["results", 0, "outcome"], "passed"),
        (["results", 0, "message"], " "),
        (["results", 1, "source"], KeyError),  # error without a source
        (["results", 1, "source"], "network"),
        (["coverage", "code", "covered"], 21),  # covered > total
        (["coverage", "code", "covered"], None),  # only one of them null
        (["coverage", "code", "total"], -1),
        (["coverage", "code", "covered"], True),
        (["coverage", "component", "basis"], ""),
        (["coverage", "system"], KeyError),
    ],
)
def test_malformed_reports_are_caught(path, value):
    assert check.report_problems(broken(path, value), "s1") != []


@pytest.mark.parametrize(
    ("body", "ok"),
    [
        ({"message": "unknown session_id", "source": "tester"}, True),
        ({"message": "", "source": "tester"}, False),
        ({"message": "x"}, False),
        ({"message": "x", "source": "client"}, False),
        ({"detail": "Not Found"}, False),
        ("plain text", False),
    ],
)
def test_error_bodies(body, ok):
    assert (check.error_body_problems(body) == []) is ok
