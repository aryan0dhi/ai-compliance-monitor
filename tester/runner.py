"""Run the contract suite against one target in a subprocess and collect its results.

A subprocess gives each run its own environment (CONTRACT_BASE_URL and keys) and contains any
crash or hang. The plugin (tester/plugin.py) streams the planned tests and each result to a
file, so an abort still leaves the results so far: the test that was running becomes
error/tester and the tests that never ran become skip.
"""

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "tests" / "contract"
# pytest exit codes 0 (all passed) and 1 (some failed) are normal; anything else is an abort.
NORMAL_EXIT_CODES = (0, 1)


@dataclass
class PlannedTest:
    test_id: str
    endpoints: list[str] = field(default_factory=list)
    acs: list[str] = field(default_factory=list)


@dataclass
class SuiteRun:
    planned: list[PlannedTest]
    results: list[dict[str, Any]]  # one per planned test, in order
    aborted: str | None = None  # why the run stopped early, or None


def suite_env(target_url: str, overrides: dict[str, str], results_file: Path, tmp: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE", "COVERAGE"))}
    env.update(overrides)
    env["CONTRACT_BASE_URL"] = target_url
    env["CONTRACT_REPORT_DIR"] = str(tmp)
    env["TESTER_RESULTS_FILE"] = str(results_file)
    return env


def run_suite(
    target_url: str,
    *,
    overrides: dict[str, str] | None = None,
    suite: Path = DEFAULT_SUITE,
    timeout_s: float = 1800,
) -> SuiteRun:
    with tempfile.TemporaryDirectory(prefix="tester-") as tmp_dir:
        tmp = Path(tmp_dir)
        results_file = tmp / "results.jsonl"
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-p",
            "tester.plugin",
            "-p",
            "no:cacheprovider",
            "-q",
            "-o",
            "addopts=",
        ]
        aborted = None
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                cmd,
                cwd=ROOT,
                env=suite_env(target_url, overrides or {}, results_file, tmp),
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            if proc.returncode not in NORMAL_EXIT_CODES:
                tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
                aborted = f"the suite stopped with pytest exit code {proc.returncode}: " + (
                    " | ".join(tail) or "no output"
                )
        except subprocess.TimeoutExpired:
            aborted = f"the run exceeded {timeout_s:g} s and was stopped"
        records = _read(results_file)
    return assemble(records, aborted)


def _read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue  # a line cut short by a crash
    return records


def assemble(records: list[dict[str, Any]], aborted: str | None) -> SuiteRun:
    planned = [
        PlannedTest(r["test_id"], r.get("endpoints", []), r.get("acs", []))
        for r in records
        if r.get("kind") == "planned"
    ]
    started = {r["test_id"] for r in records if r.get("kind") == "started"}
    finished = {r["test_id"]: r for r in records if r.get("kind") == "result"}
    if not planned and aborted is None:
        aborted = "the suite collected no tests"
    if aborted is None and len(finished) < len(planned):
        aborted = "the suite ended before every planned test reported a result"
    results = []
    for test in planned:
        result = finished.get(test.test_id)
        if result is not None:
            results.append({k: v for k, v in result.items() if k != "kind"})
        elif test.test_id in started:
            results.append(
                {
                    "test_id": test.test_id,
                    "outcome": "error",
                    "message": f"interrupted by the abort: {aborted}",
                    "source": "tester",
                }
            )
        else:
            results.append(
                {"test_id": test.test_id, "outcome": "skip", "message": "not run: the run aborted"}
            )
    return SuiteRun(planned=planned, results=results, aborted=aborted)
