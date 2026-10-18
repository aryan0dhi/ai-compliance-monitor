# Pod cross-team testing interface (v1.0.0-draft)

Every team's implementation exposes this web-service boundary from Phase 3 on, so that an
independently written test can drive any team's deployment over the network: push inputs,
control time, and observe outputs, without knowing anything about the team's internal
architecture.

- **[openapi.yaml](openapi.yaml)** (OpenAPI 3.1) is the machine-readable contract: endpoints,
  methods, request and response schemas, status codes, authentication, and the outgoing webhook
  payload. Generate clients, validators or fuzzers from it.
- **This page** holds the observable behaviour a schema can't express. The OpenAPI document
  refers to these rules as **B-xx**.

It restates the Phase 2 Section 5 contract. Items marked **Proposed** go beyond Section 5 and need
pod agreement; they're collected under [Decisions for the pod](#decisions-for-the-pod).

Key words MUST, MUST NOT, SHOULD and MAY are used as in RFC 2119.

## What is standardized, and what isn't

| Standardized (externally observable) | Left to each team |
|---|---|
| Paths, methods, JSON bodies, status codes, roles | Language, framework, database, deployment platform |
| The standard test fixtures and the reset/clock controls | How fixtures, keys and clocks are stored |
| Webhook payload and delivery guarantees | Queues, workers, retry schedules |
| Ledger **export** format and its verification rule (B-06) | How the ledger is stored or locked |
| Timing limits a tester can measure (B-04, B-21) | How fast it really is |

## Phases

| Phase | Operations (see `x-phase`) | Requirements made observable |
|---|---|---|
| 3 | `/v1/meta`, `/v1/me`, `POST /v1/metrics`, `GET /v1/events`, `GET /v1/events/{id}`, `GET /v1/events/{id}/audit`, `GET /v1/ledger/export`, `GET /v1/ledger/verify`, `POST /v1/webhooks`, `/v1/test/*` | REQ-01, 02, 03, 04, 05 (storage), 06, 13 (roles), 14 |
| 4 | `POST /v1/events/{id}/disposition`, harm fields, deadlines and revisions in `Event`, `reminder` deliveries | REQ-07, 08a, 08b, 09, 10, 11, 13, 15 |
| 5 | `GET /v1/events/{id}/report`, `POST /v1/events/{id}/report/submission` | REQ-05 (retention), 08a, 12 |

Before its phase, an operation MAY return `404` or `501`. `GET /v1/meta` says which phases a
deployment provides.

## Behaviour

### Conventions

- **B-22 Status codes.** `400` for malformed JSON, schema violations and invalid parameters
  (never `422` for these). `401` for a missing, unknown or revoked key, checked **before** the
  body, so an unauthenticated malformed request is `401`. `403` for the wrong role or system
  scope. `404` for an event id that names no event, malformed ids included. `409` for state
  conflicts. `422` only when a webhook URL is refused or fails verification. Errors are
  `{"detail": ...}` and never echo raw input.
- **B-23 Time.** Request times MUST carry an offset (naive times and Unix numbers are `400`);
  responses use UTC. All business time (detection, gap timers, deadlines, reminders, retention)
  comes from the service's clock, which the test clock controls in test deployments.

### Phase 3

- **B-01 Ingestion (REQ-01, AC-01).** `POST /v1/metrics` with a system key for the body's
  `system_id` stores the sample (`201`). A key for another system, or a human key, is `403`.
  Repeating `(system_id, metric_name, timestamp)` with the same value and type is `200` and
  creates nothing; with a different value or type it is `409` (**Proposed**, D-04).
- **B-02 Monitoring gaps (REQ-02, AC-02).** Let `t` be the service-clock arrival time of the
  system's most recent *new* sample and `k × interval` its timeout (fixtures: `3 × 300 s`).
  Once service-clock time reaches `t + k × interval` with no new sample, a `monitoring_gap` event
  for that system MUST appear within the settle time (B-21). It MUST NOT appear before:
  withholding 2 intervals never triggers. The boundary is inclusive, following Model 5 Ex-3
  (**Proposed**, D-11). At most one gap is open per system. A system with no sample since reset
  has no gap. The gap gets a `flag_notification` (B-04). When a new sample arrives, the gap
  closes: `state = GAP_CLOSED`, `gap_ended_at` = that sample's arrival, and
  `gap_duration_seconds` is set (**Proposed**, D-12). Gap events never take review states
  (INV-12).
- **B-03 Threshold breaches (REQ-03, AC-03).** A sample breaches when its value is strictly
  above (or, for a "below" threshold, strictly below) the limit in force at the sample's
  `timestamp`. A value equal to the limit never breaches. A breach returns `flagged_event_id` and
  creates an event with `type = threshold_breach`, `state = FLAGGED`, `metric_name`,
  `metric_timestamp` (the sample's timestamp), `observed_value`, `threshold_value`, `system_id`
  and `detected_at` (service-clock arrival time). One event per breaching sample (D-09 is open:
  per breach episode instead?).
- **B-04 Reviewer notification (REQ-04, AC-04).** For every new event, each webhook registered
  for `flag_notification` MUST receive a delivery (schema `WebhookDelivery`) within the maximum
  delay of `detected_at`. The default delay is 15 minutes of service-clock time. In a test
  deployment that is not advancing its clock, the delivery MUST also arrive within the settle
  time (B-21). Non-`2xx` responses are retried. Delivery is at least once; `notification_id`
  identifies duplicates.
- **B-05 Retention, storage (REQ-05).** `retain_until` ≥ `detected_at` + 6 calendar months. An
  event stays retrievable until then, including after the test clock is advanced past 5 months
  29 days (AC-05).
- **B-06 Tamper-evident ledger (REQ-06, AC-06).** `GET /v1/ledger/export` pages through every
  ledger entry in `seq` order, starting at 1 with no gaps. Each entry MUST satisfy:
  ```
  entry_hash = sha256_hex(UTF-8 of JSON({seq, entry_type, event_id, actor_id, at, payload, prev_hash}))
               with keys sorted, separators "," and ":", non-ASCII escaped (Python json.dumps(...,
               sort_keys=True, separators=(",", ":"), ensure_ascii=True))
  prev_hash  = entry_hash of entry seq-1; 64 "0" characters for seq 1
  ```
  `at` is exported exactly as hashed: UTC with microseconds and `+00:00`. Payloads contain no
  floating-point numbers. `GET /v1/ledger/verify` re-walks the chain and reports
  `valid`/`first_mismatch`. A stand-alone checker:
  [scripts/verify_ledger_export.py](../../scripts/verify_ledger_export.py) (standard library
  only). **Proposed**, D-14; the alternative is that each team publishes its own rule and
  checker.
- **B-13 Roles (REQ-13, AC-14).**

  | Role | May call |
  |---|---|
  | `system` | `POST /v1/metrics` for its own system only |
  | `viewer` | all `GET` endpoints except `/report` |
  | `reviewer` | as viewer, plus disposition and reports (Phase 4/5) |
  | `admin` | all reads, `POST /v1/webhooks`, `/v1/test/*` (**Proposed**, D-13) |
  | any key | `GET /v1/me`; `GET /v1/meta` needs no key |

- **B-14 Audit trail (REQ-14, AC-15).** Each successful `GET /v1/events/{id}` and each change to
  an event adds exactly one entry to `GET /v1/events/{id}/audit`. Reads are `action = VIEW`. Each
  entry carries the caller's `actor_id` (as returned by `/v1/me`) and the time. Listing events
  needn't be logged (D-08).

### Phase 4

- **B-07 Harm fields (REQ-07, AC-07).** Once flagged, `harm_fields` has all four Art. 3(49)
  fields with `status = PENDING` and `prefill_evidence` drawn from the event record, or `null`
  when there is none (never guessed). No field carries a severity chosen by the system.
- **B-08 Disposition (REQ-08a/b, AC-08/09).** A disposition is accepted only when all four
  fields are `CONFIRMED` or `NOT_APPLICABLE` (else `409`). `NOT_SERIOUS` needs a non-empty
  `rationale` (else `400`) and moves the event to `CLOSED` with `deadline_utc = null`. A serious
  tier moves it to `DISPOSITIONED`.
- **B-09 Revisions (REQ-09, AC-10).** Every disposition after the first appends to
  `revision_history`, keeping the previous tier, the reviewer and the time. Nothing is
  overwritten.
- **B-10 Deadlines (REQ-10, AC-11).** `deadline_utc = A + days(tier)`, where `A` is the **first**
  assignment time and days are 10 (DEATH), 2 (CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD) or 15
  (OTHER_SERIOUS). Days are calendar days (exactly 24 h each) by default. Revising to a stricter
  tier gives `min(current deadline, A + days(new tier))`. Revising to a less strict tier keeps the
  current deadline unless a `rationale` is given (otherwise `409`); with a rationale it becomes
  `A + days(new tier)`.
- **B-11 Reminders (REQ-11, AC-12).** For a serious-tier event, each webhook registered for
  `reminder` receives exactly one delivery when service-clock time reaches 50 %, 80 % and 100 % of
  the window from `A` to `deadline_utc`, and none at 49 %, 79 % or 99 %.
- **B-15 Risk indicator (REQ-15, AC-17).** `detected_at` is set once at flagging and never
  changes; it stays visible next to `deadline_utc`.

### Phase 5

- **B-12 Report (REQ-12, AC-13).** Once a serious tier is assigned, `GET /v1/events/{id}/report`
  returns the event, the four harm fields, the determination, and `detected_at`,
  `determination_at` and `assembled_at`, all non-null. Repeated calls return the same document.
  Before a serious-tier disposition the call is `409` (REQ-08a).

### Repeatable testing

- **B-20 Standard fixtures (Proposed, D-16).** After `POST /v1/test/reset` every implementation
  holds exactly these systems and thresholds and nothing else. Thresholds apply to samples
  timestamped after 2000-01-01T00:00:00Z.

  | System | `system_id` | Interval | Gap after | Metric | Type | Breaches when |
  |---|---|---|---|---|---|---|
  | loan-scorer | `550e8400-e29b-41d4-a716-446655440000` | 300 s | 3 intervals | `accuracy` | performance | `< 0.70` |
  | loan-scorer | same | | | `drift_score` | drift | `> 0.30` |
  | resume-ranker | `550e8400-e29b-41d4-a716-446655440001` | 300 s | 3 intervals | `psi` | drift | `> 0.30` |

  Reset also removes all events, samples, webhooks, notifications, dispositions, reports and
  ledger entries, and sets the simulated clock back to real time. API keys stay valid.
- **B-21 Settle time (Proposed).** Asynchronous effects (gap events, webhook deliveries,
  reminders, time-driven state changes) MUST be observable within **30 seconds of real time**
  after the request that makes them due (a metric submission or a clock advance). 10 seconds or
  less is RECOMMENDED. Testers poll for up to 30 s before declaring a failure.
- **Test routes.** `/v1/test/reset`, `/v1/test/clock/advance` and `GET /v1/test/clock` exist
  only in test deployments and MUST return `404` in production. The clock only moves forward.

## Registry entry

Each team adds its deployment to the pod registry with:

| Field | Example |
|---|---|
| team | Team A |
| base_url | `https://34-123-45-67.sslip.io` |
| interface_version, phases | from `GET /v1/meta` |
| keys | handed over privately: one `admin`, `viewer`, `reviewer`, and a `system` key per fixture system |
| webhook policy | e.g. "public https URLs only; http allowed in this test deployment" |
| contact | who to ask when a run fails |

## Conformance checks

- **Schema conformance:** run Schemathesis (or any OpenAPI fuzzer) with this `openapi.yaml`
  against a deployment, using the team's keys.
- **Behaviour:** this repository's black-box suite, `tests/contract/`, implements B-01 to B-06,
  B-13, B-14, B-20 and B-21 over HTTP and writes a coverage report by REQ, AC, endpoint, Model 1
  state and Model 4 invariant (see [docs/cross-team-testing.md](../cross-team-testing.md)).

## Decisions for the pod

| ID | Proposal | Why it matters to tests |
|---|---|---|
| D-04 | Identical repeat `200`, conflicting repeat `409` | Idempotency checks |
| D-06 | `{items, total, limit, offset}` page envelope | Every list assertion |
| D-08 | Listing events isn't audited | AC-15 counts |
| D-09 | One event per breaching sample (vs per episode) | Event and notification counts in AC-03/04/16 |
| D-11 | Gap at exactly `k × interval` (inclusive) | AC-02 boundary |
| D-12 | Gap states `GAP_OPEN`/`REVIEWER_NOTIFIED`/`GAP_CLOSED` | State assertions on gaps |
| D-13 | Admin-only webhooks and test routes | Which key a harness uses |
| D-14 | Common ledger hash rule (B-06) | Independent re-verification (Q4.3) |
| D-16 | Standard fixtures (B-20) | Every test's inputs |
| new | `/v1/meta`, `/v1/me`, `GET /v1/test/clock`, report submission, 30 s settle time (B-21) | Discovery, AC-15 identity, timing |
| D-03 | Whether `REPORT_READY`/`SUBMITTED` are visible states | Phase 5 assertions |

## Changes from Phase 2 Section 5

- Added: `GET /v1/meta`, `GET /v1/me`, `GET /v1/test/clock`,
  `POST /v1/events/{id}/report/submission`.
- Specified: every response schema, the page envelope, the webhook delivery payload and
  signature, the ledger export format and hash rule, gap event states, the standard fixtures,
  settle time, and status-code precedence.
- Unchanged: all Section 5.1 paths and their documented status codes.
