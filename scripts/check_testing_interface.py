#!/usr/bin/env python3
"""Self-test of the course testing interface: our tester against our system (CI, issue #11).

Drives the tester and the system the way graders do and fails on any wrong status code or
malformed report or error body:

1. tester POST /test/setup {target_url}             -> 201, session_id
2. system POST /test/setup with that same id        -> 409
3. tester POST /test/run                             -> 200, report validated
4. tester POST /test/run again                       -> 409
5. tester GET /test/report                           -> 200, the same body; unknown id -> 404
6. error bodies on 400 / 404 / 409 have a non-empty message and a valid source

Standard library only. Usage:
    python scripts/check_testing_interface.py --tester http://localhost:8100 \
        --system http://localhost:8000 --target-url http://api:8000 \
        --save report.json [--expect-all-pass] [--expect-code-coverage]
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

OUTCOMES = {"pass", "fail", "error", "skip"}
SOURCES = {"tester", "system", "communication", "unknown"}


class Check:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def expect(self, ok: bool, what: str) -> bool:
        print(("ok    " if ok else "FAIL  ") + what)
        if not ok:
            self.problems.append(what)
        return ok


def call(method: str, url: str, body=None, raw: bytes | None = None, timeout: float = 60):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - our URLs
            status, payload = resp.status, resp.read()
    except urllib.error.HTTPError as err:
        status, payload = err.code, err.read()
    try:
        return status, json.loads(payload)
    except ValueError:
        return status, payload.decode(errors="replace")


def error_body_problems(body) -> list[str]:
    if not isinstance(body, dict):
        return ["error body is not a JSON object"]
    problems = []
    if not isinstance(body.get("message"), str) or not body["message"].strip():
        problems.append("message missing or empty")
    if body.get("source") not in SOURCES:
        problems.append(f"source {body.get('source')!r} is not one of {sorted(SOURCES)}")
    return problems


def coverage_problems(name: str, entry) -> list[str]:
    if not isinstance(entry, dict):
        return [f"coverage.{name} missing"]
    covered, total, basis = entry.get("covered"), entry.get("total"), entry.get("basis")
    problems = []
    if not isinstance(basis, str) or not basis.strip():
        problems.append(f"coverage.{name}.basis missing or empty")
    if covered is None and total is None:
        return problems
    ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (covered, total))
    if not ints or not isinstance(covered, int) or not isinstance(total, int):
        ok = False
    else:
        ok = 0 <= covered <= total
    if not ok:
        problems.append(
            f"coverage.{name}: covered/total must be integers 0 <= covered <= total, or both null"
        )
    return problems


def report_problems(report, session_id: str) -> list[str]:
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    problems = []
    if report.get("session_id") != session_id:
        problems.append("report session_id differs")
    if not isinstance(report.get("target_url"), str) or not report["target_url"]:
        problems.append("target_url missing")
    results = report.get("results")
    if not isinstance(results, list) or not results:
        return problems + ["results missing or empty"]
    seen = set()
    for i, r in enumerate(results):
        where = f"results[{i}]"
        if not isinstance(r, dict):
            problems.append(f"{where} is not an object")
            continue
        tid = r.get("test_id")
        if not isinstance(tid, str) or not tid.strip():
            problems.append(f"{where}.test_id missing or empty")
        elif tid in seen:
            problems.append(f"{where}.test_id {tid!r} is not unique")
        seen.add(tid)
        if r.get("outcome") not in OUTCOMES:
            problems.append(f"{where}.outcome {r.get('outcome')!r} invalid")
        if not isinstance(r.get("message"), str) or not r["message"].strip():
            problems.append(f"{where}.message missing or empty")
        if r.get("outcome") == "error" and r.get("source") not in SOURCES:
            problems.append(f"{where} is an error without a valid source")
    coverage = report.get("coverage")
    if not isinstance(coverage, dict):
        return problems + ["coverage missing"]
    for name in ("code", "component", "system"):
        problems += coverage_problems(name, coverage.get(name))
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tester", required=True, help="tester base URL (as seen from here)")
    ap.add_argument("--system", required=True, help="system base URL (as seen from here)")
    ap.add_argument("--target-url", help="system base URL as the tester reaches it")
    ap.add_argument("--save", help="write the tester report here")
    ap.add_argument("--expect-all-pass", action="store_true", help="every test must pass")
    ap.add_argument(
        "--expect-code-coverage", action="store_true", help="code coverage must be measured"
    )
    ap.add_argument("--run-timeout", type=float, default=1800)
    args = ap.parse_args()
    tester, system = args.tester.rstrip("/"), args.system.rstrip("/")
    target_url = (args.target_url or args.system).rstrip("/")
    check = Check()

    def expect_error(status, body, want: int, what: str) -> None:
        if check.expect(status == want, f"{what} -> {want} (got {status})"):
            for p in error_body_problems(body):
                check.expect(False, f"{what}: error body {p}")

    # 1. Setup through the tester.
    status, body = call("POST", f"{tester}/test/setup", {"target_url": target_url}, timeout=180)
    if not check.expect(status == 201, f"tester setup -> 201 (got {status}: {body})"):
        return 1
    session_id = body.get("session_id") if isinstance(body, dict) else None
    if not isinstance(session_id, str) or not session_id:
        check.expect(False, "setup returned a session id")
        return 1

    # 2. The system already has this session.
    status, body = call("POST", f"{system}/test/setup", {"session_id": session_id, "setup": {}})
    expect_error(status, body, 409, "system setup with the tester's session id")

    # 3. Run.
    status, report = call(
        "POST", f"{tester}/test/run", {"session_id": session_id}, timeout=args.run_timeout
    )
    check.expect(status == 200, f"tester run -> 200 (got {status})")
    problems = report_problems(report, session_id) if status == 200 else ["no report"]
    for p in problems:
        check.expect(False, f"report: {p}")
    if not problems:
        check.expect(True, f"report well formed ({len(report['results'])} results)")
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
        not_passed = [r for r in report["results"] if r["outcome"] != "pass"]
        if args.expect_all_pass:
            for r in not_passed:
                check.expect(False, f"{r['test_id']}: {r['outcome']} - {r['message'][:200]}")
            check.expect(not not_passed, f"all {len(report['results'])} tests passed")
        if args.expect_code_coverage:
            code = report["coverage"]["code"]
            check.expect(
                code["covered"] is not None and code["covered"] > 0,
                f"code coverage measured ({code['covered']}/{code['total']}: {code['basis']})",
            )

    # 4. A second run of the same session.
    status, body = call("POST", f"{tester}/test/run", {"session_id": session_id})
    expect_error(status, body, 409, "second tester run")

    # 5. The stored report, and an unknown session.
    status, stored = call("GET", f"{tester}/test/report?session_id={session_id}")
    check.expect(status == 200, f"tester report -> 200 (got {status})")
    check.expect(stored == report, "stored report is exactly the run's report")
    unknown = urllib.parse.quote(str(uuid.uuid4()))
    status, body = call("GET", f"{tester}/test/report?session_id={unknown}")
    expect_error(status, body, 404, "tester report for an unknown session")
    status, body = call("GET", f"{system}/test/report?session_id={session_id}")
    check.expect(status == 200, f"system report for the session -> 200 (got {status})")
    if status == 200:
        for p in coverage_problems("code", (body.get("coverage") or {}).get("code")):
            check.expect(False, f"system report: {p}")

    # 6. Error bodies.
    status, body = call("POST", f"{tester}/test/setup", raw=b"not json")
    expect_error(status, body, 400, "tester setup with a malformed body")
    status, body = call("POST", f"{tester}/test/setup", {"target_url": "ftp://nowhere"})
    expect_error(status, body, 400, "tester setup with a non-http target")
    status, body = call("POST", f"{tester}/test/run", {"session_id": unknown})
    expect_error(status, body, 404, "tester run for an unknown session")
    status, body = call("GET", f"{tester}/test/report")
    expect_error(status, body, 400, "tester report without session_id")
    status, body = call("POST", f"{system}/test/setup", {"setup": {}})
    expect_error(status, body, 400, "system setup without session_id")
    status, body = call("GET", f"{system}/test/report?session_id={unknown}")
    expect_error(status, body, 404, "system report for an unknown session")

    print(f"\n{len(check.problems)} problem(s)")
    return 1 if check.problems else 0


if __name__ == "__main__":
    sys.exit(main())
