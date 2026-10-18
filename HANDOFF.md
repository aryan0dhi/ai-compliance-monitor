# Handoff: metrics, thresholds and event storage (REQ-01, REQ-03, REQ-05 storage)

**Owner:** Aryan · **Branch:** `feature/metrics-database` · **Last updated:** 2026-10-17

This is for the developers building authentication, audit logging, notifications, monitoring-gap
detection, the background worker and the test routes. It covers what exists, how to plug in, and
what is still open. The Phase 2 submission is the source of truth; where it is ambiguous, see
[docs/spec-decisions.md](docs/spec-decisions.md).

## 1. Status

| Area | State |
|---|---|
| REQ-01 metric ingestion `POST /v1/metrics` | Implemented, unit + contract tested |
| REQ-03 thresholds (above/below, strict) and FLAGGED events | Implemented, unit + contract tested (AC-03 boundaries included) |
| REQ-03 threshold configuration | Service function only (`add_threshold_version`); no endpoint (D-07) |
| `GET /v1/events`, `GET /v1/events/{id}` | Implemented, unit + contract tested |
| REQ-05 storage (`retain_until`, constraint, RESTRICT FKs) | Implemented. **Retention enforcement/purge is Phase 5 and not implemented** |
| Real-PostgreSQL integration tests | **114 pass** on PostgreSQL 16 (2026-10-18), plus Schemathesis on PostgreSQL with the real auth, ledger and notifier |
| Pod decisions D-01, D-03 to D-16 | Interim behavior implemented; agreement pending (backlog §10.2, spec-decisions D-11 to D-16) |
| REQ-13 auth (API keys, roles), REQ-06/14 ledger, REQ-04 notifications, REQ-02 gaps, test routes, worker | **Implemented 2026-10-18** (§4 describes how; [docs/cross-team-testing.md](docs/cross-team-testing.md) for podmates) |

What a caller sees today: a valid key is required (`401` otherwise; issue keys with
`python -m app.cli`). A breach stores the event, its `FLAGGED` ledger entry and an outbox row in
one transaction, and the worker delivers the notification to registered webhooks. Silence for 3
intervals opens a gap event.

## 2. Getting started

```bash
cp .env.example .env            # set POSTGRES_PASSWORD
docker compose up --build -d    # api + postgres, runs migrations on start
docker compose exec api pytest  # all tests; REQUIRE_POSTGRES=1 makes DB tests fail, not skip
```

Without Docker:

```bash
python3.12 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
pytest                          # unit + contract tests; integration tests skip
pytest -m fuzz                  # Schemathesis fuzzing of /v1 (FUZZ_EXAMPLES=1000 for a deep run)
ruff check . && ruff format --check . && mypy
bandit -q -r app && pip-audit --skip-editable
```

## 3. How a request flows

```
HTTP ─► router.py ──► service.py ──────────────► repository.py (SQL)
        (parse,       (business logic; takes      Protocol + Sql* implementation
         map errors)   uow, ledger, notifier,
                       clock as arguments)
                          │
                          ├─► ledger.append(uow.session, ...)     ┐ all inside ONE
                          └─► notifier.enqueue(uow.session, ...)  ┘ transaction (`with uow:`)
```

- **Unit of work** (`app/db/uow.py`): `with uow:` opens one transaction and gives access to
  `uow.systems`, `uow.metrics`, `uow.thresholds`, `uow.events`. A clean exit commits; any
  exception rolls everything back. This is how Q7.2 Decision 1 (state change + ledger entry +
  outbox row, all or nothing) is enforced.
- **Dependencies**: routes get every collaborator through FastAPI `Depends`: `get_principal`,
  `get_uow`, `get_ledger`, `get_notifier`, `get_clock`. Tests replace them with
  `app.dependency_overrides`; production wiring is never patched.

## 4. Integration interfaces

All live in `app/modules/<module>/interface.py`. **Keep the Protocol signatures**; callers depend
on them. If one must change, update `tests/fakes.py` too (mypy checks the doubles against the
Protocols, so drift fails CI).

> **Update 2026-10-18:** all of the interfaces below are now implemented with the production
> classes noted in each subsection; `tests/test_interfaces.py::test_production_wiring_uses_real_integrations`
> asserts the wiring. The original guidance is kept because it is still the contract the
> implementations follow.

### 4.1 Authentication (REQ-13): `app/modules/auth/interface.py`

Implemented: `get_principal` looks up the sha256 of the bearer key (`app_user` + `api_key`, migration 0003); keys come from `python -m app.cli create-user`.

```python
def get_principal() -> Principal          # FastAPI dependency; currently raises 401
class Principal: actor_id: UUID; role: Role; system_id: UUID | None
class Role: SYSTEM | VIEWER | REVIEWER | ADMIN
```

- Contract (Section 5): `Authorization: Bearer <api_key>`. System keys are scoped to one
  `system_id`; human keys carry viewer/reviewer (admin per Q7.1).
- You may add parameters to `get_principal` (e.g. `authorization: str = Header(...)`, a DB
  session). Callers only use `Depends(get_principal)`.
- **Must stay fail-closed**: any missing/invalid/unknown key is `401`, never a default principal.
- How my code uses it:
  - `POST /v1/metrics` requires `role == SYSTEM` and `system_id == body.system_id`, else `403`.
  - `GET /v1/events*` allows `VIEWER`, `REVIEWER`, `ADMIN`; system keys get `403` (D-06).
  - `actor_id` is written to ledger entries (`FLAGGED` uses the system key's actor).
- Model 2 leftovers for you: store API keys hashed (Q7.3); add the FK from
  `threshold_version.changed_by` to your users table in your migration.
- Auth runs before body validation, so an unauthenticated malformed request gets `401`, not
  `400`. Keep it that way (tested in `tests/test_api.py::TestProductionWiring`).

### 4.2 Audit ledger (REQ-06, REQ-14, REQ-16): `app/modules/ledger/interface.py`

Implemented: `SqlLedgerWriter` (hash chain, D-14), `/v1/ledger/export`, `/v1/ledger/verify`, `/v1/events/{id}/audit`. REQ-16 (crypto-shredding) is Phase 5.

```python
class LedgerWriter(Protocol):
    def append(self, session, *, entry_type: str, actor_id: UUID | None,
               payload: dict, event_id: UUID | None = None) -> None
def get_ledger() -> LedgerWriter          # returns UnimplementedLedger today (raises -> 501)
```

- **Use the `session` you are given and never commit or roll back**; the caller's unit of work
  owns the transaction. The event row is already flushed, so an FK to `event.id` works.
- Entry types I write today (`LedgerEntryType`): `FLAGGED` (on breach, payload = event snapshot
  with decimals as strings and ISO timestamps) and `VIEW` (on `GET /v1/events/{id}`, payload
  `{"event_id": ...}`). Add the rest of Model 2's types to the enum.
- If `append` raises, the breach or view fails and nothing is stored. That's intended (no
  unaudited state change or view).
- Open for you: **D-08** (should listing events be logged?). Your endpoints
  (`/v1/events/{id}/audit`, `/v1/ledger/export`, `/v1/ledger/verify`) go in your module's router,
  included from `app/api/v1.py`.

### 4.3 Notifications (REQ-04, REQ-11): `app/modules/notifications/interface.py`

Implemented: `OutboxNotifier`, `POST /v1/webhooks` (with SSRF guard), delivery by the worker (D-15). REQ-11 reminders are Phase 4.

```python
class Notifier(Protocol):
    def enqueue(self, session, *, notification_type: str, event_id: UUID, payload: dict) -> None
def get_notifier() -> Notifier            # returns UnimplementedNotifier today (raises -> 501)
```

- Insert an **outbox row** with the given `session` (same transaction as the event); the worker
  delivers it later (Q7.2 Decision 2). Don't send anything inline.
- I call it once per new breach with `notification_type="flag_notification"` and the same payload
  as the ledger entry. The gap sweeper should call it too (REQ-02).
- `POST /v1/webhooks` belongs to you. Its "verification ping" fetches a caller-supplied URL;
  guard against SSRF (block private/link-local ranges, limit redirects and timeouts).
- **D-09** affects you: today every breaching sample creates an event, and so a notification.

### 4.4 Monitoring-gap detection (REQ-02)

Implemented in `app/modules/monitoring_gaps/service.py`. One deviation from the note below: the boundary is inclusive (`>=`), following Ex-3 and AC-02 (D-11). Gap states are Model 1's (D-12).

There is no interface to implement; you build on the shared tables:

- **Input**: `monitored_system.last_seen_at` (the server **receive** time of the most recent
  *new* sample; identical repeats don't update it) and `sampling_interval_s` (default 300).
  REQ-02's timeout is "3 missed intervals", so a gap is `now - last_seen_at > 3 *
  sampling_interval_s`, using `clock.now()`. `last_seen_at` is `NULL` until the first sample.
- **Creating a gap event**: insert an `Event` with `type="monitoring_gap"`, `state` per D-03,
  `system_id`, `detected_at=clock.now()` and `retain_until=retain_until_for(detected_at)`. Leave
  the breach columns (`metric_name`, `metric_timestamp`, `observed_value`,
  `threshold_version_id`, `threshold_value`) `NULL`; the CHECK constraint only requires them for
  `threshold_breach`. Do it inside a `with uow:` block with `uow.events.add(event)`, then
  `ledger.append(...)` and `notifier.enqueue(...)` with `uow.session`, like
  `events.service.create_breach_event`.
- **Gap columns** (start/end, duration for Ex-3): add them in your own migration (nullable, or
  enforced with a CHECK on `type = 'monitoring_gap'`).
- **Resumption**: poll `last_seen_at` from the worker; no hook in ingestion is needed.
- INV-12: gap events must never enter review or get a disposition. A `type` check in Phase 4 code
  is the simplest guard.

### 4.5 Test routes, reset and clock (Section 5.1, 5.3)

Implemented in `app/modules/testing/` (D-16). The simulated clock is stored in the database so the API and worker agree.

- **Clock**: everything reads time through `app.core.clock.get_clock()`. Implement the simulated
  clock and install it with `set_clock(...)` in the test environment. My code only ever calls
  `clock.now()`.
- **Routes**: include your router in `app/api/v1.py` **only when
  `get_settings().test_routes_enabled`** (it's false whenever `APP_ENV=production`), so the routes
  404 in production.
- **Reset fixtures**: create systems as `MonitoredSystem` rows. Create thresholds with
  `thresholds.service.add_threshold_version(...)` (it enforces insert-only and the type rules),
  and always pass a `direction` (D-01). Truncation order respecting the RESTRICT FKs:
  `event`, `threshold_version`, `metric_sample`, `monitored_system` (plus your tables first).

### 4.6 Background worker

Implemented: `app/modules/worker/runner.py`, run as `python -m app.worker` (docker-compose
`worker` service). Originally: Run it as a second docker-compose service from the same image (placeholder comment
in `docker-compose.yml`) with a single active instance (advisory lock, Q7.2). Use `get_uow()` /
`SqlAlchemyUnitOfWork` and `get_clock()` exactly as the API does.

## 5. Functions you can reuse

| Function | Module | Purpose |
|---|---|---|
| `ingest_metric(submission, *, principal, uow, ledger, notifier, clock)` | `metrics.service` | Full REQ-01/03 flow (e.g. for an Evidently adapter) |
| `create_breach_event(uow, *, sample, threshold, detected_at, actor_id, ledger, notifier)` | `events.service` | Event + ledger + notification in the caller's transaction |
| `list_events(filters, *, limit, offset, principal, uow)` / `get_event(event_id, *, principal, uow, ledger)` | `events.service` | Event queries (`get_event` logs a VIEW) |
| `add_threshold_version(uow, *, system_id, metric_name, metric_type, direction, limit_value, valid_from, changed_by)` | `thresholds.service` | Configure a threshold (append-only) |
| `threshold_in_force(repo, system_id, metric_name, at)`, `is_breach(threshold, value)` | `thresholds.service` | Detection primitives |
| `retain_until_for(detected_at)` | `events.models` | REQ-05 retention date (UTC, matches the DB constraint) |
| `Direction.breaches(value, limit)` | `thresholds.models` | Strict above/below comparison |

## 6. Rules that keep the modules compatible

- **Time**: never `datetime.now()` or SQL `now()` for business logic; use `clock.now()`.
  Store `timestamptz` and keep datetimes timezone-aware.
- **Numbers**: metric values and limits are `Decimal` / `numeric`, never float.
- **Transactions**: integrations take the caller's `session` and never commit. One `with uow:`
  per use case.
- **Enums**: text + CHECK (`app.db.base.check_in`). Changing event states/types means a
  migration that alters `ck_event_state` / `ck_event_type`, plus `EventState`/`EventType` and D-03.
- **Foreign keys**: `ON DELETE RESTRICT`; reference events with
  `event_id uuid NOT NULL REFERENCES event(id) ON DELETE RESTRICT`.
- **Migrations**: one linear history. Wrap explicit constraint/index names in `op.f(...)`, or
  Alembic applies the naming convention a second time (`tests/test_migrations.py` catches this).
  If two branches add a revision, run `alembic merge` before pushing. 0001 and 0002 are on
  `feature/metrics-database` only and have not been applied anywhere yet.
- **New repositories**: add them to the `UnitOfWork` Protocol, `SqlAlchemyUnitOfWork` and
  `tests/fakes.py::FakeUnitOfWork` (mypy enforces the match).
- **Errors**: return `400` for invalid input (the global handler maps validation errors). Don't
  echo raw input back in error bodies.

## 7. Testing patterns

- **Unit/service tests**: use `tests/fakes.py` (`FakeUnitOfWork` restores state on exception,
  `RecordingLedger`/`RecordingNotifier` record calls and can be told to fail, `FixedClock`).
- **HTTP tests**: `create_app()` plus `app.dependency_overrides`, as in `tests/test_api.py`.
  Override with a callable that *returns* a double (`lambda: RecordingLedger()`); passing the
  class itself makes FastAPI treat its constructor arguments as query parameters.
- **Integration tests** (`tests/integration/`): each session creates a throwaway database
  `compliance_it_<random>`, migrates it, and drops it afterwards. Use the `make_uow` and `seed`
  fixtures. The role needs `CREATEDB` (the compose superuser has it).
- **Fuzzing**: `tests/test_fuzz.py` runs Schemathesis over `/v1` with the doubles. Add your
  endpoints' error codes to their `responses=` so the schema stays accurate.

## 8. Open decisions

See [docs/spec-decisions.md](docs/spec-decisions.md). The *Agenda* section lists D-01 to D-10
in priority order. The ones that affect your modules: **D-03** (state names, Phase 4),
**D-07** (configuration and fixture format, testing), **D-08** (ledger), **D-09** (notification
volume), **D-10** (gap semantics for odd timestamps).

## 9. Security: what has and hasn't been verified

HawkScan (DAST) was skipped: there is no running deployment, no `HAWK_API_KEY`, and auth isn't
implemented, so a scan would only see `401`s.

**Done (2026-10-16/17):**
- bandit (SAST) on `app/`: 0 issues after replacing one `assert` with an explicit check.
- pip-audit: no vulnerable application dependencies. `pip` 25.2 itself has advisories; the
  Dockerfile now upgrades it.
- Schemathesis, 1,000 generated requests per endpoint with test doubles: no server errors and
  no schema violations. It found two real defects, both fixed with regression tests: numeric
  timestamps were read as Unix time, and the published schema didn't state the `metric_name`
  rules.
- Edge-case probing found and fixed: NUL/control characters in `metric_name`, edge whitespace
  hiding breaches, huge integers turning into `null`, and timestamp overflow causing `500`.
- SQL is parameterized throughout (SQLAlchemy; no string-built queries in `app/`).
- No secrets in git history or tracked files; `.env` is gitignored.
- Schemathesis against **real PostgreSQL** (`tests/integration/test_fuzz_pg.py`, 1,000 requests
  per endpoint): clean. It found one more defect, now fixed with a regression test:
  `offset` above PostgreSQL's bigint range caused a `500`.
- pip-audit inside the built API image: no known vulnerabilities (Python packages only).

**Verified 2026-10-18 (Phase 3 integrations):** auth fails closed on every endpoint (unit,
fuzz and live contract run); `/v1/test/*` are `404` with `APP_ENV=production` even if
`ENABLE_TEST_ROUTES` is set; webhook SSRF guard (private/loopback/link-local/metadata/IPv4-mapped
addresses refused, address pinned, no redirects, no proxies) with a real-socket test; ledger
tamper detection by direct SQL edit/delete (AC-06, Ex-5); bandit clean; pip-audit clean on both
lockfiles; Schemathesis 1,000 requests per endpoint on fakes and PostgreSQL.

**Still unverified (needs a running deployment and/or Docker):**

| Check | Why it's open | Owner |
|---|---|---|
| Brute force / rate limiting on API keys | Keys are 256-bit random (brute force infeasible) and stored hashed; no rate limit yet | team |
| SQL injection and other DAST checks (HawkScan) | No running app plus `HAWK_API_KEY` | team |
| Security headers (HSTS, `X-Content-Type-Options`, ...) and TLS on Lightsail | Not configured; deployment not set up | deployment |
| Request body size limits and rate limiting on `POST /v1/metrics` | None configured | team |
| CORS policy for the reviewer web UI | Not configured (browsers block cross-origin by default) | UI owner |
| `/docs` and `/openapi.json` exposed in production | Decide whether to disable when `APP_ENV=production` | team |
| Webhook secrets stored in clear (needed for HMAC signing) | Encrypt at rest with REQ-16's key handling | Phase 5 |
| Container image OS (Debian) packages | pip-audit only covers Python packages; run an image scanner (e.g. `docker scout cves`) | team |

To run HawkScan later: start the stack (`docker compose up`), set `HAWK_API_KEY`, configure
`stackhawk.yml` with the base URL and a test API key once auth exists, then run the
`hawkscan` loop.

## 10. Backlog for this area

### 10.1 Docker / PostgreSQL verification (done 2026-10-17)

Run on Docker Desktop (engine 29.1.3) with `postgres:16-alpine`, inside the api container
(`REQUIRE_POSTGRES=1`, so nothing could silently skip).

- [x] `docker compose up --build -d`: the api container ran `alembic upgrade head`
      (0001 → 0002), `alembic current` reports `0002 (head)`, `GET /healthz` returns `200`.
- [x] `docker compose exec api pytest -m integration`: **52 passed**. The first run had 3
      failures, all caused by the `violates()` test helper discarding successful inserts; fixed
      in the helper, schema unchanged. Covered:
      - migration upgrade → downgrade → upgrade round trip
      - live autogenerate check: models and migrations have no drift
      - every CHECK, UNIQUE and RESTRICT foreign key, including the six-month retention
        boundary under UTC, New York and Kiritimati session time zones
      - `retain_until_for()` matching PostgreSQL interval arithmetic at month ends and leap days
      - 8 concurrent identical submissions → exactly 1 sample and 1 event
      - ingestion, threshold configuration, event queries and HTTP on real SQL
- [x] Schemathesis on PostgreSQL (`pytest -m "integration and fuzz"`, 1,000 requests per
      endpoint): clean after fixing the `offset` bigint overflow it found.
- [x] Full container suite: **226 passed, 0 skipped**.

To re-run: `docker compose up --build -d && docker compose exec api pytest`.

Machine note: Docker Desktop first failed because its disk image (`Docker.raw`) was owned by
root, and then because the filesystem inside it was corrupted. The fix was to `chown` it and
move it aside (`Docker.raw.broken-2026-10-17`, safe to delete). Docker Desktop 4.56.0 still
shows an available update.

### 10.2 Blocked on pod decisions ([docs/spec-decisions.md](docs/spec-decisions.md))

Current behavior is implemented and tested; the "If changed" column is the follow-up work.

| ID | Decision needed | Current behavior | If changed |
|---|---|---|---|
| D-06 | Pagination shape, ordering, who may read events, 400 for bad queries, input rules | `limit`/`offset` envelope, newest first, viewer/reviewer/admin only | Update `events/router.py`, `events/schemas.py`, `tests/test_api.py` |
| D-09 | One event per breaching sample, or one per breach episode | One per sample | New episode logic in `metrics/service.py` + `events/repository.py`; AC-03/04/16 fixtures |
| D-04 | Status codes for repeated samples | `TEMPORARY(D-04)`: `200` identical, `409` conflicting | Change `_resolve_duplicate` and the router mapping; search `TEMPORARY(D-04)` |
| D-07 | Configuration API and the shared fixture format | `add_threshold_version()` service only; systems seeded as rows | Add admin endpoints and/or a shared fixture loader |
| D-01 | Shared fixtures must give each threshold a direction | `above`/`below`, strict | Confirm only; no code change expected |
| D-03 | Event state/type names (Section 5 vs Models 1–2) | Section 5 names | Migration altering `ck_event_state`/`ck_event_type` + enums (before Phase 4) |
| D-08 | Whether listing events writes ledger entries | Only `GET /v1/events/{id}` logs a `VIEW` | Pass the ledger to `list_events` (with the ledger owner) |
| D-05 | Evidence-bundle window and when to hash it | `evidence_bundle_hash` NULL, not computed | Compute the hash (flag time or worker); Phase 5 retention depends on it |
| D-10 | Limits on future or very old timestamps | Any valid ISO-8601 accepted | Add a window check in `metrics/schemas.py` using the clock |

### 10.3 Later phases for this area (not started)

- [ ] Phase 5 retention (REQ-05): purge data after `retain_until`, keep each event's
      evidence-window samples until then (D-05), and document the 7-day Lightsail backup
      retention. Today only storage is implemented (column + minimum-six-months constraint +
      RESTRICT FKs); nothing prevents or performs early deletion.
- [ ] Unbounded growth: no request size limit or rate limit on `POST /v1/metrics` (see §9).
