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

**Status:** all 226 tests pass in docker compose, including 52 against real PostgreSQL
(2026-10-17). Several contract details still await pod agreement; see
[HANDOFF.md §10](HANDOFF.md#10-backlog-for-this-area).

## Stack

Python 3.12, FastAPI, PostgreSQL 16, SQLAlchemy 2, Alembic, Docker Compose. Hosted on AWS Lightsail.

## Quick start (Docker)

```bash
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up --build -d
curl http://localhost:8000/healthz
```

On startup, the API container runs `alembic upgrade head` and then serves on port 8000 with
auto-reload, with `app/`, `alembic/` and `tests/` mounted from your checkout.

- API docs: http://localhost:8000/docs
- Health: `GET /healthz` returns `200 {"status":"ok","database":"ok"}`, or `503` when the database
  is unreachable.

Useful commands:

```bash
docker compose logs -f api                           # structured JSON logs
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
pytest                                               # integration tests skip without a DB
```

## Configuration

All settings come from environment variables (see [.env.example](.env.example)), read by
[app/core/config.py](app/core/config.py). Either set `DATABASE_URL`, or set the individual
`POSTGRES_*` variables (Lightsail managed PostgreSQL also needs `POSTGRES_SSLMODE=require`).
`ENABLE_TEST_ROUTES` mounts the `/v1/test/*` routes and is always ignored when
`APP_ENV=production`. Secrets come only from the environment and are never committed.

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
```

| Module | Scope | Owner |
|---|---|---|
| `systems` | `monitored_system` (shared) | shared |
| `metrics` | REQ-01 metric ingestion, `POST /v1/metrics` | Aryan |
| `thresholds` | REQ-03 threshold config and breach detection | Aryan |
| `events` | REQ-03 event creation, REQ-05 storage, `GET /v1/events` | Aryan (Phase 4 review: TBD) |
| `ledger` | REQ-06/14/16 hash-chained audit ledger, `/v1/ledger/*` | teammate |
| `notifications` | REQ-04/11 outbox, webhooks, reminders | teammate |
| `monitoring_gaps` | REQ-02 gap detection (worker) | teammate |
| `auth` | REQ-13 API keys and roles | teammate |
| `testing` | `/v1/test/clock/advance`, `/v1/test/reset` | teammate |

Each module grows `models.py`, `schemas.py`, `service.py` and `router.py` as needed. Modules talk
to each other only through the `interface.py` files below.

### Integration interfaces (not implemented yet)

Under Q7.2 Decision 1, a state change, its ledger entry and its outbox row commit in **one
transaction**. These interfaces define how modules call each other. They are deliberately **not**
no-ops:

| Interface (FastAPI dependency) | Behavior until implemented |
|---|---|
| `modules/auth/interface.py` `get_principal()` → `Principal` | **Fails closed**: every protected request gets `401`. |
| `modules/ledger/interface.py` `get_ledger()` → `LedgerWriter.append(session, ...)` | `UnimplementedLedger` raises `UnimplementedError` (shown to API callers as `501`) and aborts the transaction. |
| `modules/notifications/interface.py` `get_notifier()` → `Notifier.enqueue(session, ...)` | `UnimplementedNotifier` raises `UnimplementedError` and aborts the transaction. |
| `core/clock.py` `get_clock()` → `Clock.now()` | Real UTC. The testing module will swap in the simulated clock. |
| `db/uow.py` `get_uow()` → `UnitOfWork` | Implemented: one transaction plus the module repositories. |

Owners replace the `Unimplemented*` class (or the body of `get_principal`) and keep the Protocol
signature. If a signature has to change, coordinate with the people calling it.
`tests/test_interfaces.py` asserts that the production defaults stay fail-closed until then.

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
| `GET /v1/events/{event_id}` | REQ-03, REQ-15, REQ-05 storage | Full event; records a `VIEW` ledger entry (REQ-14). |
| `GET /healthz` | Q7.3 | Database connectivity. |

Until auth exists, every `/v1` call returns `401`. Once auth exists, a breach and an event view
return `501` until the ledger and notifications modules are implemented. Open contract details
are listed in [docs/spec-decisions.md](docs/spec-decisions.md) (D-04, D-06, D-09).

Thresholds are configured with `thresholds.service.add_threshold_version()`. Section 5 has
no configuration endpoint yet (D-07).

## Checks

```bash
pytest                          # unit + HTTP contract tests; integration tests skip without a DB
pytest -m integration           # real-PostgreSQL tests (docker compose exec api pytest -m integration)
pytest -m fuzz                  # Schemathesis fuzzing of /v1 (FUZZ_EXAMPLES=1000 for a deep run)
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
(REQ-01, REQ-03, storage part of REQ-05).

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
