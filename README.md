# ai-compliance-monitor

Post-market monitoring service for high-risk AI systems under the EU AI Act (Articles 72–73).
The service collects performance and drift metrics, flags threshold breaches and monitoring gaps,
notifies human reviewers and keeps a tamper-evident audit history. Severity decisions always stay
with a human reviewer.

The Phase 2 specification is the source of truth for requirements (REQ-xx), acceptance criteria
(AC-xx), the `/v1` API contract and the data model. Where its parts disagree, see
[docs/spec-decisions.md](docs/spec-decisions.md).

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
pytest -m "not integration"                          # unit tests only, no DB needed
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

| Interface | Behavior until implemented |
|---|---|
| `modules/auth/interface.py` `get_principal()` | **Fails closed**: every protected request gets `401`. |
| `modules/ledger/interface.py` `append(session, ...)` | Raises `UnimplementedError` (shown to API callers as `501`) and aborts the transaction. |
| `modules/notifications/interface.py` `enqueue(session, ...)` | Raises `UnimplementedError` and aborts the transaction. |
| `core/clock.py` `get_clock().now()` | Real UTC. The testing module will swap in the simulated clock. |

Owners replace the body of their interface function and keep the signature. If a signature has
to change, coordinate with the people calling it.

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
