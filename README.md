# ai-compliance-monitor

Post-market monitoring service for high-risk AI systems under the EU AI Act (Articles 72–73).
The service collects performance and drift metrics, flags threshold breaches and monitoring gaps,
notifies human reviewers and keeps a tamper-evident audit history. Severity decisions always stay
with a human reviewer.

The Phase 2 specification is the source of truth for requirements (REQ-xx), acceptance criteria
(AC-xx), the `/v1` API contract and the data model. Where its parts disagree, see
[docs/spec-decisions.md](docs/spec-decisions.md).

**Joining the project or building a module that plugs into metrics/events? Start with
[HANDOFF.md](HANDOFF.md).** It covers the interfaces to implement, reusable functions,
compatibility rules, testing patterns, open decisions and unverified security checks.

**Status (2026-10-18):** Phase 3 scope is implemented: REQ-01, 02, 03, 04, 06, 14, the storage
part of REQ-05 and the Phase 3 role checks of REQ-13, plus the `/v1/test/*` routes and the
background worker. 503 tests pass against PostgreSQL 16 (95.7% line coverage), and the black-box
contract suite passes 27/27 against a live API + worker. **Cross-team testing:** see
[docs/cross-team-testing.md](docs/cross-team-testing.md). Several contract details still await
pod agreement ([docs/spec-decisions.md](docs/spec-decisions.md)).

## Stack

Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2, Alembic, Docker Compose. Hosted on AWS Lightsail.

## Quick start (Docker)

```bash
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up --build -d
curl http://localhost:8000/healthz
```

On startup, the API container runs `alembic upgrade head` and then serves on port 8000 with
auto-reload, with `app/`, `alembic/` and `tests/` mounted from your checkout. The `worker`
container (same image, `python -m app.worker`) starts once the API is healthy and runs gap sweeps
and webhook delivery every `WORKER_POLL_INTERVAL_S`.

- API docs: http://localhost:8000/docs
- Health: `GET /healthz` returns `200 {"status":"ok","database":"ok","worker":"ok"}` (`worker` is
  `stale` or `missing` if its heartbeat stops), or `503` when the database is unreachable.

### API keys

Every `/v1` call needs `Authorization: Bearer <api_key>`. Section 5 has no account endpoints, so
keys are issued with the admin CLI (printed once; only a hash is stored):

```bash
docker compose exec api python -m app.cli create-system --name loan-scorer --id 550e8400-e29b-41d4-a716-446655440000
docker compose exec api python -m app.cli create-user --name loan-scorer-key --role system --system-id 550e8400-e29b-41d4-a716-446655440000
docker compose exec api python -m app.cli create-user --name alice --role viewer   # or reviewer / admin
docker compose exec api python -m app.cli add-threshold --system-id <id> --metric drift_score --type drift --direction above --limit 0.30
docker compose exec api python -m app.cli revoke-key --key-id <key_id>
docker compose exec api python -m app.cli verify-ledger                             # exit 1 if tampered
```

Useful commands:

```bash
docker compose logs -f api worker                    # structured JSON logs
docker compose exec api pytest                       # all tests, including DB integration
docker compose exec api alembic current              # current migration revision
docker compose down                                  # stop (keeps data)
docker compose down -v                               # stop and delete the local database
```

## Local development without Docker

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d db                              # just the database
alembic upgrade head
uvicorn app.main:app --reload
python -m app.worker                                 # in a second terminal
pytest                                               # integration tests skip without a DB
```

## Configuration

All settings come from environment variables (see [.env.example](.env.example)), read by
[app/core/config.py](app/core/config.py). Either set `DATABASE_URL`, or set the individual
`POSTGRES_*` variables (Lightsail managed PostgreSQL also needs `POSTGRES_SSLMODE=require`).
`ENABLE_TEST_ROUTES` mounts the `/v1/test/*` routes and switches the API and worker to the shared
simulated clock; it is always ignored when `APP_ENV=production`. `NOTIFY_MAX_DELAY_S` (REQ-04,
default 900) and the worker/webhook settings are listed in `.env.example`. Secrets come only from
the environment and are never committed.

## Project layout

```
app/
  main.py               app factory; mounts /healthz and /v1
  core/                 config, clock interface, JSON logging, shared errors
  db/                   SQLAlchemy Base, engine/session, model registry for Alembic
  api/                  /healthz and the /v1 router aggregator
  modules/              one package per requirement area (below)
alembic/                migrations
tests/
docs/database.md        schema, relationships, conventions
docs/spec-decisions.md  spec conflicts and interim decisions
docs/cross-team-testing.md  how podmates test us, how we test them, results log
scripts/                start-api.sh; verify_ledger_export.py (standalone ledger re-verification)
```

| Module | Scope |
|---|---|
| `systems` | `monitored_system` (shared) |
| `metrics` | REQ-01 metric ingestion, `POST /v1/metrics` |
| `thresholds` | REQ-03 threshold config and breach detection |
| `events` | REQ-03 event creation, REQ-05 storage, `GET /v1/events` (Phase 4 review: TBD) |
| `auth` | REQ-13 API keys (hashed) and roles |
| `ledger` | REQ-06/14 hash-chained audit ledger, `/v1/ledger/*`, `/v1/events/{id}/audit` (REQ-16: Phase 5) |
| `notifications` | REQ-04 outbox, `POST /v1/webhooks`, delivery (REQ-11 reminders: Phase 4) |
| `monitoring_gaps` | REQ-02 gap detection |
| `worker` | background loop: gap sweeps, webhook delivery, REQ-04 overdue alerts, heartbeat |
| `testing` | `/v1/test/clock/advance`, `/v1/test/clock`, `/v1/test/reset`, baseline fixtures |

Each module grows `models.py`, `schemas.py`, `service.py` and `router.py` as needed. Modules talk
to each other only through the `interface.py` files below.

### Integration interfaces

Under Q7.2 Decision 1, a state change, its ledger entry and its outbox row commit in **one
transaction**. Modules call each other only through these FastAPI dependencies (tests replace them
with `app.dependency_overrides`):

| Interface | Production implementation |
|---|---|
| `modules/auth/interface.py` `get_principal()` → `Principal` | API-key lookup by sha256; **fails closed** (`401`) for missing, unknown or revoked keys |
| `modules/ledger/interface.py` `get_ledger()` → `LedgerWriter.append(session, ...)` | `SqlLedgerWriter`: hash-chained entry in the caller's transaction |
| `modules/notifications/interface.py` `get_notifier()` → `Notifier.enqueue(session, ...)` | `OutboxNotifier`: outbox row in the caller's transaction; the worker delivers it |
| `core/clock.py` `get_clock()` → `Clock.now()` | Real UTC, or the shared simulated clock in the test environment |
| `db/uow.py` `get_uow()` → `UnitOfWork` | One transaction plus the module repositories |

`tests/test_interfaces.py` asserts this wiring and that auth stays fail-closed.

### Code layout inside a module

```
router.py      HTTP only: parse/validate, call the service, map errors to status codes
schemas.py     request/response bodies (Section 5.2)
service.py     business logic; framework-independent, takes a UnitOfWork + collaborators
repository.py  SQL behind a Protocol (Sql*Repository is the production implementation)
models.py      SQLAlchemy tables
```

Services receive every collaborator (unit of work, ledger, notifier, clock) as an argument. That
makes them reusable from other entry points (e.g. an Evidently adapter or the worker) and
testable with the explicit doubles in `tests/fakes.py`. mypy checks that those doubles match the
production Protocols.

## Implemented endpoints

| Endpoint | Requirements | Notes |
|---|---|---|
| `POST /v1/metrics` | REQ-01, REQ-03 | Stores the sample; on a strict threshold breach (above/below) creates a `FLAGGED` event and returns `flagged_event_id`. Duplicate handling is temporary (D-04). |
| `GET /v1/events` | REQ-03 | Filters `system_id`, `state`, `type`, `since`; `limit`/`offset` pagination (D-06). |
| `GET /v1/events/{event_id}` | REQ-03, REQ-15, REQ-05 storage | Full event (gap events include start/end/duration); records a `VIEW` ledger entry (REQ-14). |
| `GET /v1/events/{event_id}/audit` | REQ-14 | `[{actor_id, action, timestamp, seq}]` for one event. |
| `GET /v1/ledger/export` | REQ-06 | Raw entries and hashes, paged with `after_seq`; re-verify with `scripts/verify_ledger_export.py`. |
| `GET /v1/ledger/verify` | REQ-06 | `{valid, entries_checked, first_mismatch, head_seq, head_hash}`. |
| `POST /v1/webhooks` | REQ-04 | Admin only; verification ping (`422` on failure); returns the HMAC signing secret once. |
| `POST /v1/test/clock/advance`, `GET /v1/test/clock`, `POST /v1/test/reset` | Section 5.1 | Test environment only (`404` in production); admin only. |
| `GET /healthz` | Q7.3 | Database connectivity and worker heartbeat. |

Monitoring gaps (REQ-02) have no endpoint of their own: the worker creates `monitoring_gap`
events, which appear in `GET /v1/events?type=monitoring_gap`. Roles, open contract details and
interim choices: [docs/spec-decisions.md](docs/spec-decisions.md) (D-04, D-06, D-09, D-11 to D-16).

Thresholds are configured with `python -m app.cli add-threshold` (or
`thresholds.service.add_threshold_version()`). Section 5 has no configuration endpoint yet (D-07).

## Checks

```bash
pytest                          # unit + HTTP contract tests; integration tests skip without a DB
pytest -m integration           # real-PostgreSQL tests (docker compose exec api pytest -m integration)
pytest -m fuzz                  # Schemathesis fuzzing of /v1 (FUZZ_EXAMPLES=1000 for a deep run)
pytest tests/contract -m contract  # black-box suite against a live service (CONTRACT_BASE_URL)
ruff check . && ruff format --check .
mypy                            # app/ and the test doubles' Protocol conformance
bandit -q -r app                # static security analysis
pip-audit --skip-editable       # known-vulnerable dependencies
```

Integration tests create and drop their own throwaway database. With `REQUIRE_POSTGRES=1`
(set in docker-compose) they fail instead of skipping when PostgreSQL is unreachable.

## Database schema

Tables, relationships, conventions and how to extend them: [docs/database.md](docs/database.md).
Migration `0002` adds `monitored_system`, `metric_sample`, `threshold_version` and `event`
(REQ-01, REQ-03, storage part of REQ-05). Migration `0003` adds accounts and keys, the ledger, the
notification outbox and webhooks, gap columns and states, the test clock and the worker heartbeat.

## Database migrations

```bash
# 1. Add/modify models in app/modules/<name>/models.py
# 2. Make sure the module is imported in app/db/models.py
docker compose exec api alembic revision --autogenerate -m "add metric_sample"
# 3. Review the generated file in alembic/versions/, then:
docker compose exec api alembic upgrade head
```

Four people share one linear migration history. If two branches each add a migration, `alembic
heads` shows two heads after merging. Fix that with `alembic merge -m "merge heads" <rev1> <rev2>`
before pushing. Never edit a migration that has already been merged to `main`.
