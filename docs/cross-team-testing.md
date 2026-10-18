# Cross-team testing (Phase 3, Q6.1)

Phase 3 asks each team to run its full test suite against every podmate's service at least once,
to have a podmate run their suite against ours, and to record the results, what happened when
something didn't work, and code, component and system-model coverage **for each direction**.
This page covers both directions and is where the results go.

- [1. Testing our service (podmates)](#1-testing-our-service-podmates)
- [2. Testing a podmate's service (us)](#2-testing-a-podmates-service-us)
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

## 3. Measuring server-side code coverage

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
