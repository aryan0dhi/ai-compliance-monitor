# Cross-team testing (Phase 3, Q6.1)

Phase 3 asks each team to run its full test suite against every podmate's service at least once,
to have a podmate run their suite against ours, and to record the results, what happened when
something didn't work, and code, component and system-model coverage **for each direction**.
This page covers both directions and is where the results go.

- [1. Testing our service (podmates)](#1-testing-our-service-podmates)
- [1a. Course testing interface: `/test/setup` and `/test/report`](#1a-course-testing-interface-testsetup-and-testreport)
- [2. Testing a podmate's service (us)](#2-testing-a-podmates-service-us)
- [2a. Tester service: `/test/setup`, `/test/run`, `/test/report`](#2a-tester-service-testsetup-testrun-testreport)
- [3. Measuring server-side code coverage](#3-measuring-server-side-code-coverage)
- [4. Results log](#4-results-log)

## 1. Testing our service (podmates)

### What we give you

| Item | Value |
|---|---|
| Base URL | *to be filled in after the test deployment* |
| Contract | Phase 2 Section 5 (`/v1`), plus the interim choices in [spec-decisions.md](spec-decisions.md) |
| Keys | One `system` key per baseline system, one `viewer`, one `admin` (sent privately, never in git) |
| Interactive docs | `<base URL>/docs` (OpenAPI; use "Authorize" with `Bearer <key>`) |

Every request needs `Authorization: Bearer <api_key>`. A missing or unknown key gets `401`. A
key with the wrong role gets `403`.

| Role | May call |
|---|---|
| `system` | `POST /v1/metrics` for its own `system_id` only |
| `viewer`, `reviewer` | `GET /v1/events`, `GET /v1/events/{id}`, `GET /v1/events/{id}/audit`, `GET /v1/ledger/*` |
| `admin` | all reads, plus `POST /v1/webhooks` and `/v1/test/*` (D-13) |

### Baseline fixtures (after `POST /v1/test/reset`)

From the specification's own examples. Thresholds apply to samples timestamped after
2000-01-01, so any current timestamp works. A value exactly at the limit never flags (AC-03).

| System | `system_id` | Interval | Metric | Type | Breaches when |
|---|---|---|---|---|---|
| loan-scorer | `550e8400-e29b-41d4-a716-446655440000` | 300 s | `accuracy` | performance | `< 0.70` |
| loan-scorer | same | | `drift_score` | drift | `> 0.30` |
| resume-ranker | `550e8400-e29b-41d4-a716-446655440001` | 300 s | `psi` | drift | `> 0.30` |

Reset clears events, metrics, webhooks, notifications and the ledger. It also sets the clock back
to real time. API keys survive a reset, so call it at the start of every run.

### Behaviour to expect

- **Breach** (REQ-03): `POST /v1/metrics` returns `201` with `flagged_event_id`. The event is
  `FLAGGED` and records the metric name, timestamp, value, threshold and system.
- **Notification** (REQ-04): register your receiver with `POST /v1/webhooks` (admin key). We
  `POST` it once before saving to check it answers `2xx`; otherwise you get `422`. Each delivery
  is a JSON body `{notification_id, notification_type, event_id, created_at, deliver_by, event}`
  with header `X-Signature-SHA256: sha256=<HMAC of the body, keyed by the secret you received>`.
  Delivery is **at least once**, so deduplicate on `notification_id`. The worker polls every 5 s,
  so expect deliveries within seconds. The REQ-04 limit is 15 min.
- **Gap** (REQ-02): after the last sample, `≥ 3 × interval` with no new data opens a
  `monitoring_gap` event, and its notification goes to the same webhooks. Exactly 3 intervals
  triggers (Ex-3); 2 intervals doesn't (AC-02, D-11). Gap states are `GAP_OPEN` →
  `REVIEWER_NOTIFIED` (after webhook delivery) → `GAP_CLOSED` (when data resumes, with
  `gap_duration_seconds`) (D-12). A system that has never sent data has no gap.
- **Test clock**: `POST /v1/test/clock/advance {"seconds": 900}` moves the service *and* the
  worker forward. Wait about 10 s for the next worker pass before checking for gaps.
- **Audit** (REQ-14): `GET /v1/events/{id}/audit` returns `[{actor_id, action, timestamp, seq}]`.
  Every `GET /v1/events/{id}` adds a `VIEW` (AC-15). `actor_id` is `null` for the worker.
- **Ledger** (REQ-06): `GET /v1/ledger/verify` returns
  `{valid, entries_checked, first_mismatch, head_seq, head_hash}`. `GET /v1/ledger/export?after_seq=`
  pages through the raw entries. To verify independently (Q4.3), run
  `python scripts/verify_ledger_export.py --base-url <URL> --api-key <viewer key> --save l.json`
  (standard library only). Edit any payload in `l.json` and run it again to see the tamper
  detected (AC-06).

## 1a. Course testing interface: `/test/setup` and `/test/report`

Testers and graders use these two endpoints around a run. They sit at the base URL (not under
`/v1`), exist only in the test environment, and need **no API key**. The `/v1` calls made during
the run still need the keys above. Full schema:
[testing-interface/system-openapi.yaml](testing-interface/system-openapi.yaml). Choices: D-19.

```bash
# 1. Start a session: resets to the baseline fixtures, clock at real time, coverage from zero.
curl -X POST "$BASE/test/setup" -H 'Content-Type: application/json' \
     -d '{"session_id": "grader-42", "setup": {}}'
# -> 201 {"session_id": "grader-42"}

# 2. ... run the tests against $BASE/v1 with the keys ...

# 3. Code coverage of what the run reached (reading it changes nothing).
curl "$BASE/test/report?session_id=grader-42"
# -> 200 {"session_id": "grader-42",
#         "coverage": {"code": {"covered": 1234, "total": 2100, "basis": "line coverage of app/ ..."}}}
```

| Status | When | `source` |
|---|---|---|
| `400` | Not a JSON object, empty or missing `session_id`, `setup` not an object, no `session_id` query | `tester` |
| `404` | Unknown session (also after an API restart) | `tester` |
| `409` | `session_id` already used, another setup still in progress, or report before setup finished | `tester` |
| `500` | The reset failed (for example, the database is down) | `system` |

Every error body is `{"message", "source"}`. One session is active at a time; a new setup ends
the previous one, and its report stays readable with the numbers it had at that moment. Coverage
counts only the API process (not the worker), so it can be lower than the figure from section 3.
If it can't be measured, `covered` and `total` are `null` with the reason in `basis`.

## 2. Testing a podmate's service (us)

`tests/contract/` is a black-box suite that talks only HTTP to the Section 5 contract, so it runs
against any pod service. Ask the podmate for a base URL, a system key, a viewer key and an admin
key, and tell them which fixture their system/metrics correspond to.

```bash
export CONTRACT_BASE_URL=https://their-service.example.com
export CONTRACT_SYSTEM_KEY=... CONTRACT_VIEWER_KEY=... CONTRACT_ADMIN_KEY=...
# Only if their fixtures differ from ours (defaults shown):
export CONTRACT_SYSTEM_ID=550e8400-e29b-41d4-a716-446655440000
export CONTRACT_DRIFT_METRIC=drift_score CONTRACT_DRIFT_LIMIT=0.30
export CONTRACT_PERF_METRIC=accuracy CONTRACT_PERF_LIMIT=0.70
# Their service must be able to reach the webhook receiver this run starts:
export CONTRACT_RECEIVER_BIND=0.0.0.0:8099 CONTRACT_RECEIVER_URL=https://<tunnel-to-8099>/hook
#   ...or skip the webhook checks:  export CONTRACT_WEBHOOKS=off
export CONTRACT_REPORT_DIR=reports/contract-<team>-$(date +%F)
pytest tests/contract -m contract -v
```

The run writes `contract-report.md` and `contract-report.json`, and the results log below uses
them. Each test declares the REQs, ACs, endpoints (**component coverage**), and Model 1 states and
Model 4 invariants (**system-model coverage**) it exercises. The report lists what was covered,
attempted but failing, and never exercised. Tests that depend on a choice the pod hasn't agreed
yet carry its id (for example D-06 for the pagination shape). A failure there is a contract
mismatch to discuss, not necessarily a defect. If their service has no `/v1/test/*` routes, the
time-dependent AC-02 check is skipped and reported as such.

Without our codebase, the only black-box measure of a podmate's service is component and
system-model coverage. **Code coverage of their service is theirs to measure**: ask them to run
their server under coverage (as in section 3) during our run and share the figure.

## 2a. Tester service: `/test/setup`, `/test/run`, `/test/report`

The course interface's tester side wraps the same contract suite in an HTTP service that
graders can call. It's a separate app (`tester/`, `uvicorn tester.main:app`), served at
`<base URL>/tester` on the test deployment and on port 8100 locally
(`docker compose --profile tester up -d tester`). Schema:
[testing-interface/tester-openapi.yaml](testing-interface/tester-openapi.yaml). No key is needed
to call it.

```bash
T=https://<site>/tester
# 1. New session (UUID). The tester calls the target's POST /test/setup and waits for it.
curl -X POST "$T/test/setup" -H 'Content-Type: application/json' \
     -d '{"target_url": "https://<target system base URL>"}'
# -> 201 {"session_id": "4f6c..."}
# 2. Run the suite against the target's /v1 API (takes a few minutes); returns the report.
curl -X POST "$T/test/run" -H 'Content-Type: application/json' -d '{"session_id": "4f6c..."}'
# 3. Read the stored report again, unchanged.
curl "$T/test/report?session_id=4f6c..."
```

**Report.** `results` lists each planned test once, in run order, as
`{test_id, outcome, message, source?}`:

| Outcome | Meaning |
|---|---|
| `pass` | Passed, including checks that expect a project error (a `401`, `403`, `404`, ...) |
| `fail` | An assertion about the target's behaviour failed |
| `error` | It couldn't complete. `source` says why: `system` for an unexpected 5xx, `communication` for a timeout or connection failure, `tester` for a problem in the suite or its setup |
| `skip` | Not applicable (for example, webhook checks when no receiver is reachable) or never run because the run aborted |

`coverage.code` comes from the target's `GET /test/report`. If that is unavailable or malformed,
the tester reports `null` with the reason. `coverage.component` counts the Phase 3 pod-interface
endpoints, and `coverage.system` counts the Phase 3 acceptance criteria, that at least one
passing test exercised. Both use the `@covers` tags, with the lists in `tests/contract/scope.py`.

**Statuses.** Setup returns `400` (no valid http(s) `target_url`). It passes a target's
well-formed `400`/`404`/`409`/`500` through unchanged. It returns `500` with `system` when the
target answers anything else, and `500` with `communication` when the target can't be reached.
A failed setup creates no session. Run returns `404` (unknown session) or `409` (already run or
running). If the run aborts (crash, or longer than `TESTER_RUN_TIMEOUT_S`, default 30 min), it
returns `500` with `tester`, and the results so far are stored: the interrupted test is
`error`/`tester` and the rest are `skip`. Report returns `409` until the run has ended. Every
error body is `{message, source}`.

**Keys for the target.** The tester's own `CONTRACT_SYSTEM_KEY`, `CONTRACT_VIEWER_KEY` and
`CONTRACT_ADMIN_KEY` (and the other `CONTRACT_*` settings from section 2) apply to every target.
Per-target values go in a JSON file named by `TESTER_TARGETS_FILE`:
`{"https://their-system.example": {"CONTRACT_SYSTEM_KEY": "...", "CONTRACT_SYSTEM_ID": "..."}}`.
Keys are never part of a URL or the registry.

**Limits.** Sessions and reports are kept in memory until the tester stops. Each run is its own
pytest process. Two sessions against the same target at once would reset each other's data, so
run one session per target at a time. On the test deployment the webhook checks are skipped
(`CONTRACT_WEBHOOKS=off`), because no target can deliver webhooks into the VM.

**CI self-test.** The contract CI job also starts the tester and runs
`scripts/check_testing_interface.py` against our own system, the way graders call the
interface. It fails on a wrong status code, a malformed report or error body, any test that
doesn't pass, or unmeasured code coverage. The report is uploaded as the
`testing-interface-report` artifact. Schemathesis checks both OpenAPI documents in the regular
test run (`tests/test_testing_interface_schemas.py`).

## 3. Measuring server-side code coverage

`GET /test/report` (section 1a) gives per-session numbers for the API process without a restart.
The deployed test stack builds with `INSTALL_COVERAGE=true` for that. For a combined API + worker
figure, use the compose override below instead. Under it, `/test/report` returns `null` and
points here, because two coverage measurements can't share a process.

When a podmate runs their suite against our test deployment, measure which of our code it reached
by running the API and worker under `coverage` (installed in the dev image):

```yaml
# docker-compose.coverage.yml (in the repo root; usage is in its header comment)
services:
  api:
    command: ["sh", "-c", "alembic upgrade head && exec coverage run -p -m uvicorn app.main:app --host 0.0.0.0 --port 8000"]
    environment: { COVERAGE_FILE: /srv/coverage/.coverage }
    volumes: ["./coverage-data:/srv/coverage"]
  worker:
    command: ["coverage", "run", "-p", "-m", "app.worker"]
    environment: { COVERAGE_FILE: /srv/coverage/.coverage }
    volumes: ["./coverage-data:/srv/coverage"]
```

After their run: `docker compose stop api worker`. Coverage saves on SIGTERM; see
`[tool.coverage.run]` in pyproject.toml. Then run
`coverage combine coverage-data && coverage report`. The `coverage-data` directory must be
writable by uid 1000. This compose override wasn't run here because there was no Docker on the
machine. The same measurement was verified with an equivalent in-process harness; see the
baseline below.

## 4. Results log

One entry per direction per run. Copy the coverage tables from `contract-report.md`.

### Baseline: our contract suite against our own service (2026-10-18)

Not a cross-team run; it shows the suite and the service work end to end before podmates use
them. Setup: local PostgreSQL 16, API (uvicorn) and worker as separate processes,
`APP_ENV=test`, `ENABLE_TEST_ROUTES=true`, `WEBHOOK_ALLOW_PRIVATE_NETWORKS=true`, keys issued with
`python -m app.cli`, webhook receiver on localhost.

**Result: 27 passed, 0 failed, 0 skipped.**

| Coverage | Ratio |
|---|---|
| Phase 3 requirements (REQ-01, 02, 03, 04, 05, 06, 13, 14) | 8/8 |
| Phase 3 acceptance criteria (AC-01, 02, 03, 04, 06, 15) | 6/6 |
| Endpoints (component) | 9/9 |
| Model 1 states: FLAGGED, DENIED, GAP_OPEN, REVIEWER_NOTIFIED, GAP_CLOSED (system model) | 5/5 |
| Model 4 invariants: INV-7, 8, 10, 12, 13 (system model) | 5/5 |
| Server-side code coverage of `app/` during the run | 82% |

Server-side code not reached is mostly code no HTTP client can reach: the admin CLI, the worker's
process entry point and leader election, system/threshold configuration (D-07), and error paths
such as failed retries. The in-repo suites cover those (95.7% line coverage overall).

### Direction A: our suite → *podmate service* (date)

- Target / keys / fixture mapping:
- Result: passed / failed / skipped
- What didn't work, and what happened (contract mismatch vs. defect, D-xx involved, follow-up):
- Coverage: component | system model | their server-side code coverage (if they measured it)

### Direction B: *podmate suite* → our service (date)

- Their suite / runner:
- Result:
- What didn't work, and what happened:
- Coverage: their component/system-model figures | our server-side code coverage (section 3)
