# Spec decisions and open questions

The Phase 2 submission (pod specification of Sept 25, 2026, plus our team's Sections 7–9) is the
source of truth. This file records where its parts disagree or leave behavior open, and what this
codebase does about it. Items marked **pod** need agreement with the other team before they are
final, because cross-team tests depend on them.

| ID | Topic | Status |
|----|-------|--------|
| D-01 | Threshold direction (above/below) | Decided (team); **pod** to confirm for shared fixtures |
| D-02 | `metric_type` column and `metric_name` naming | Decided (team); no API impact |
| D-03 | Event type/state names: Section 5 vs Models 1–2 | Interim, **pod** |
| D-04 | Duplicate metric submissions | Open, **pod**; temporary behavior |
| D-05 | Evidence bundle window (±1 h) vs flag-time hashing | Open, **pod** |
| D-06 | API details Section 5 leaves unspecified (Phase 3 endpoints) | Interim, **pod** |
| D-07 | Configuration endpoints (thresholds, systems) are missing from Section 5 | Open, **pod** |
| D-08 | INV-8 (every read is logged) vs Section 5 (listing has no state change) | Open, **pod** + audit owner |
| D-09 | Repeated breaches: one event per breaching sample vs one per episode | Open, **pod** |
| D-10 | Timestamp plausibility (future/very old samples) | Open, **pod** |
| D-11 | Gap boundary: "longer than" 3 intervals vs Ex-3 detecting at exactly 3 | Decided (team, follows Ex-3); **pod** to confirm |
| D-12 | Monitoring-gap event states | Interim, **pod** (part of D-03) |
| D-13 | Roles for endpoints Section 5 doesn't assign (webhooks, test routes) | Interim, **pod** |
| D-14 | Ledger hash algorithm, and which reads are logged | Decided (team); **pod** to confirm for re-verification |
| D-15 | Webhook delivery semantics (fan-out, retries, signature, SSRF rules) | Interim, **pod** |
| D-16 | Test environment: shared clock, reset scope, fixtures | Interim, **pod** (with D-07) |
| D-17 | AC-01 cadence: what happens to a sample more than 10 s off schedule | Interim, **pod** |
| D-18 | AC-05 checkpoints: calendar 5mo29d can fall after the 6-month minimum | Open, **pod** |

## Agenda for the next pod meeting

Ordered by impact on cross-team testing:

1. **D-06**: pagination shape, who may read events, `400` for invalid queries. The other team's
   harness will call these endpoints directly.
2. **D-09**: one event per breaching sample (current) or one per breach episode. This changes
   how many events and notifications AC-03/AC-04/AC-16 should expect.
3. **D-04**: status codes for duplicate submissions (`200`/`409` now).
4. **D-07**: how thresholds are configured, and the shared fixture format for
   `/v1/test/reset`.
5. **D-01**: shared fixtures must say whether each threshold is `above` or `below`.
6. **D-03**: state names before Phase 4 (`DISPOSITIONED` vs `CLASSIFIED`, etc.).
7. **D-08**: whether listing events writes ledger entries.
8. **D-05** and **D-10**: needed before Phase 5 retention and evidence hashing.
9. **D-11** to **D-16**: added with gap detection, notifications, auth, the ledger and the test
   routes (Phase 3). D-13 and D-16 matter first, because the other team's harness needs keys,
   fixtures and the test clock.

## D-01: Threshold direction (REQ-03, AC-03)

**Conflict.** REQ-03 and AC-03 say a metric breaches when it "exceeds" its threshold, and a value
exactly at the threshold must not flag. Model 5 Ex-1 flags `accuracy = 0.61` against a threshold of
`0.70`, a value *below* the threshold. Model 2's `threshold_version` has no column for direction.

**Decision.** Add `direction` (`above` | `below`) to `threshold_version`. A breach is strictly
`value > limit_value` for `above` and strictly `value < limit_value` for `below`. A value equal to
the threshold never flags (AC-03 boundary).

**Still open (pod).** AC-03 says "one unit above must flag", which is undefined for floating-point
metrics. The shared fixtures should state concrete boundary values instead.

## D-02: `metric_type` and `metric_name` (REQ-01, Section 5.2)

**Conflict.** The API's MetricSample requires `metric_type` (`performance` | `drift`), but Model 2's
`metric_sample` table has no such column. The table names the column `metric`, while the API uses
`metric_name`.

**Decision.** `metric_sample` stores `metric_type`, and the column is named `metric_name` to match
the API. The primary key becomes `(system_id, metric_name, sampled_at)`.

## D-03: Event type and state names (REQ-02, REQ-03, Model 1, Section 5)

**Conflict.** The Section 5 contract and Models 1–2 use different names:

| Concept | Section 5 API (used in code) | Model 1 / Model 2 |
|---|---|---|
| Event type | `threshold_breach`, `monitoring_gap` | `BREACH`, `GAP` |
| Flagged | `FLAGGED` | `FLAGGED` |
| Under review | `UNDER_REVIEW` | `UNDER_REVIEW` |
| Serious tier assigned | `DISPOSITIONED` | `CLASSIFIED(tier)` |
| Assessed not serious | `CLOSED` | `CLOSED_NOT_SERIOUS` |
| Report assembled | (none) | `REPORT_READY` |
| Exported by reviewer | (none) | `SUBMITTED` |
| Gap states | (none; gaps use the same `state` field) | `GAP_OPEN` → `REVIEWER_NOTIFIED` → `GAP_CLOSED` |
| Disposition tier | `CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD` | `CRIT_INFRA` |

**Interim decision.** The database and API use the Section 5 names, because the cross-team harness
checks those values. Phase 3 creates `FLAGGED` breach events; gap events use their own states (D-12). Nothing past `FLAGGED` is implemented for breaches yet.

**To agree with the pod before Phase 4.**
- Whether `REPORT_READY` and `SUBMITTED` become API-visible states or remain internal.
- Which `state` values a `monitoring_gap` event reports (INV-12 says a gap never enters review).
- The canonical spelling of the critical-infrastructure tier.

## D-04: Duplicate metric submissions (REQ-01, Section 5.1)

**Ambiguity.** Model 2 makes ingestion idempotent on `(system_id, metric_name, sampled_at)`, but
Section 5 only defines `201`, `400`, `401` and `403` for `POST /v1/metrics`. It does not say what a
repeated submission of the same key returns, whether a different `value` for the same key is an
error, or whether a duplicate can create a second flagged event.

**Temporary implementation (marked `TEMPORARY(D-04)` in `app/modules/metrics/service.py` and
`router.py`).**
- The first submission is stored and evaluated against thresholds as normal (`201`).
- A repeat with the same key, `metric_type` and value (numerically equal, so `0.35` = `0.350`)
  returns **`200`** with the stored sample and its `flagged_event_id`, if any. Nothing new is
  created, and neither `received_at` nor `last_seen_at` changes.
- A repeat with the same key but a different value or `metric_type` returns **`409`** and does not
  overwrite the stored sample.
- A duplicate never creates a second event. This is enforced in code and by the
  `uq_event_triggering_sample` unique constraint.
- Concurrent identical submissions are race-safe: the insert is
  `INSERT ... ON CONFLICT DO NOTHING`.
- These status codes are interim and will be replaced by whatever the pod agrees on.

**Question for the pod.** Should an identical repeat return `200` or `201`? Should a conflicting
repeat return `409` (not currently in the contract)?

## D-05: Evidence bundle window (REQ-05, Model 2)

**Conflict.** Model 2 defines `event.evidence_bundle_hash` as the sha256 of an evidence bundle of
"±1 h of samples" around the breach. The hour *after* detection doesn't exist yet when the event is
flagged, so the hash can't be computed at flag time. Model 1 also says the evidence bundle is
created when the event enters FLAGGED.

**Options.**
- (a) Bundle = samples in `[t − 1 h, t]`, hashed at flag time. Simple and atomic with the flag, but
  loses post-breach context.
- (b) Keep ±1 h and compute the hash once `t + 1 h` has passed. Background worker job; the event
  carries a null hash for the first hour.
- (c) Hash `[t − 1 h, t]` at flag time and add a second hash for the full window later.

**Interim decision.** `event.evidence_bundle_hash` is **nullable** and **not computed** in Phase 3.
A CHECK constraint ensures that any value written is a 64-character lowercase hex sha256.

**To agree with the pod.** Which window, when the hash is computed, and which module owns it.
Phase 5 retention also depends on this, because the samples in an event's window must be kept
until that event's `retain_until`.

## D-06: API details Section 5 leaves unspecified (Phase 3 endpoints)

Implemented as below and open to change by pod agreement. Each choice favors the strictest
reading of the contract, or least privilege, when the contract is silent.

| Topic | Section 5 says | Interim behavior |
|---|---|---|
| Pagination format | "paginated array of Event summaries" | `limit` (1–200, default 50) and `offset` (0 to 2⁶³−1, PostgreSQL's bigint limit) query params. Response `{"items": [...], "total", "limit", "offset"}` |
| List ordering | not specified | Newest `detected_at` first; `event_id` breaks ties so pages are stable |
| Event summary fields | not specified | `event_id, system_id, type, state, detected_at, metric_name` |
| Invalid list query (bad `state`, naive `since`, `limit=0`, ...) | only `401` listed | `400`, same as malformed bodies |
| Who may read events | only `401` listed | `viewer`, `reviewer`, `admin`. Monitored-system keys get `403` (they only submit metrics) |
| Who may submit metrics | system keys "are used for metric submission" | Only a system key whose `system_id` matches the body. Human keys get `403` |
| Malformed `event_id` in path | `404` if it doesn't exist | `404` (a malformed id can't exist) |
| `flagged_event_id` when no breach | "also contains" on breach | Omitted from the response |
| `value` | number | JSON number only. Strings, booleans, `NaN` and infinities get `400`. Stored as exact `numeric` |
| `timestamp` | ISO-8601 UTC | ISO-8601 string with an offset. Naive values and numbers (which a lenient parser would read as Unix time) get `400`. Other offsets are accepted and normalized to UTC; responses use `Z`. Values that fall outside year 1–9999 once converted to UTC get `400` |
| `metric_name` | string | 1–255 characters, no leading/trailing whitespace, no control characters (published as a `pattern` in the OpenAPI document). Whitespace variants would silently miss their threshold |
| `since` (GET /v1/events) | not specified | Compared with `detected_at` (inclusive). ISO-8601 with an offset only |
| `system_id` | "UUID/string" in MetricSample, UUID in Event | UUID only (`400` otherwise) |
| Unknown `system_id` with a valid key | not specified | `400` (should not occur once auth exists) |
| `metric_type` differs from the configured threshold's | not specified | `400`, nothing stored. Silently mixing types could hide a breach |
| Unknown fields in the body | not specified | Ignored |
| Threshold used for a late sample | not specified | The version in force at the sample's `timestamp`, not at arrival |
| Viewing an event (`GET /v1/events/{id}`) | adds a view to the audit log | Writes a `VIEW` ledger entry in the same transaction; if the ledger write fails, the request fails |
| Listing events | "State Change: None" | No ledger entries |
| `deadline_utc`, `harm_fields`, `revision_history` | Event fields | Always `null` / `null` / `[]` until Phase 4 |

## D-07: Configuration endpoints (REQ-01, REQ-03, Section 6.1)

**Gap.** Section 6.1 lists "configuration" endpoints in the Phase 3 contract, and REQ-01/REQ-03
refer to a *configurable* sampling interval and *configured* thresholds. Section 5 defines no
endpoint for creating monitored systems, setting `sampling_interval_s`, or adding thresholds.

**Current behavior.**
- Thresholds: `thresholds.service.add_threshold_version()` is the supported way to configure
  one. It appends a new version (never updates), requires `valid_from` later than the latest
  version, keeps `metric_type` consistent, and rejects non-finite limits. There is no HTTP
  endpoint.
- Systems and sampling interval: rows are created directly (e.g. by `/v1/test/reset`
  fixtures). There is no service function or endpoint yet.

**To agree with the pod.** Whether configuration is part of the cross-team contract (e.g.
`POST /v1/systems/{id}/thresholds`, admin role) or only seeded through `/v1/test/reset`, and the
shared fixture format (systems, thresholds with `direction`, API keys).

## D-08: Logging reads of the event list (REQ-14, INV-8, Section 5.1)

**Conflict.** INV-8 says every read of an event record *through any endpoint* produces exactly
one ledger entry. Section 5.1 says `GET /v1/events` has no state change and only states the
audit side effect for `GET /v1/events/{id}`.

**Current behavior.** Only `GET /v1/events/{id}` writes a `VIEW` entry; listing writes none.

**To agree.** If listing must be logged: one entry per returned event, or one `LIST` entry with
the filter and returned ids? This mostly affects the ledger owner (volume, AC-15).

## D-09: Repeated breaches (REQ-03, REQ-04, AC-16)

**Ambiguity.** REQ-03 says "when a collected metric exceeds its threshold, generate a flagged
event". A drift score that stays above its threshold for an hour at the default 5-minute
interval produces 12 breaching samples.

**Current behavior.** One event per breaching sample (the literal reading of REQ-03), so 12
events and 12 reviewer notifications.

**Options.** (a) Keep one per sample. (b) One event per *episode*: while an event for the same
`(system_id, metric_name)` is still `FLAGGED`/`UNDER_REVIEW`, later breaches attach to it
instead of creating new events. (b) needs a rule for when an episode ends, and changes what
AC-03/AC-04/AC-16 count.

## D-10: Timestamp plausibility (REQ-01, AC-01)

**Gap.** Section 5 doesn't bound the metric `timestamp`. Samples dated far in the future (clock
skew, bugs) are accepted today. They are judged against the threshold in force at that future
time and stored with `received_at` = now. `last_seen_at` uses `received_at`, so gap detection
isn't affected.

**To agree.** Whether to reject timestamps more than N minutes ahead of the server clock (the
test clock in the test environment), and whether very old backfilled samples should be
accepted.

## D-11: Gap boundary (REQ-02, AC-02, Model 5 Ex-3)

**Conflict.** REQ-02 says a gap is raised when data stops "for longer than" the timeout (3 missed
intervals). Ex-3 detects the gap at exactly 10:15 after the last sample at 10:00, and AC-02 says
withholding exactly 3 intervals must trigger.

**Decision.** Inclusive: a gap exists when `now - last_seen_at >= gap_missed_intervals *
sampling_interval_s`. Withholding 2 intervals never triggers. `gap_missed_intervals` is a
per-system setting (default 3). Systems that have never sent a sample aren't monitored yet and
can't be in a gap.

## D-12: Monitoring-gap event states (REQ-02, Model 1, INV-12)

**Gap.** Section 5 lists only breach states. Model 1 gives gaps their own path:
`GAP_OPEN` → `REVIEWER_NOTIFIED` → `GAP_CLOSED`.

**Interim decision.** Gap events use Model 1's gap states. The database pairs states with types
(`ck_event_state_matches_type`), so a gap can never be `UNDER_REVIEW` or receive a disposition
(INV-12). `REVIEWER_NOTIFIED` is set when a webhook first accepts the gap notification.
`GAP_CLOSED` is set when a new sample arrives, with `gap_ended_at` = that sample's arrival time
(Ex-3's duration). If data resumes before delivery, the gap goes straight to `GAP_CLOSED`. At most
one open gap per system (`uq_event_one_open_gap_per_system`).

## D-13: Roles for endpoints Section 5 doesn't assign (REQ-13)

| Endpoint | Section 5 says | Interim rule |
|---|---|---|
| `GET /v1/events/{id}/audit`, `GET /v1/ledger/*` | "viewer can read events/audit/ledger" | `viewer`, `reviewer`, `admin`; system keys `403` |
| `POST /v1/webhooks` | nothing | `admin` only: registering a URL that receives event data is a configuration change |
| `/v1/test/*` | nothing | `admin` only, and only mounted in the test environment (`404` in production) |

Keys are issued with `python -m app.cli create-user` (Section 5 has no account endpoint; see
D-07). Only the sha256 of each key is stored.

## D-14: Ledger hashing and logged reads (REQ-06, REQ-14, INV-7, INV-8)

**Decision (team).** `entry_hash = sha256(canonical JSON of seq, entry_type, event_id, actor_id,
at, payload, prev_hash)`, with sorted keys, no whitespace, ASCII escapes and `at` in UTC with
microseconds. The first `prev_hash` is 64 zeros. Appends are serialized with a transaction
advisory lock, and `seq` is gapless (assigned by the writer, not a sequence). The full rule is
in `app/modules/ledger/chain.py`, reimplemented in `scripts/verify_ledger_export.py`.

- Modifying, deleting or reordering any entry is detected. Removing entries from the *end* is
  only detected against a previously recorded `head_seq`/`head_hash` (the verify response returns
  both). Keeping the head outside the database is Phase 5.
- Logged reads: `GET /v1/events/{id}` writes `VIEW`. Listing (D-08), the audit trail and ledger
  export/verify are not logged; they show audit records rather than event records, and logging
  them would grow the ledger on every verification pass.
- `actor_id` has no FK to `app_user`, so an export verifies on its own.

## D-15: Webhook delivery (REQ-04, Section 5.1)

**Interim behaviour.**
- An outbox row is written in the event's transaction with `deliver_by = detection +
  NOTIFY_MAX_DELAY_S` (900 s). The worker fans it out to every active webhook subscribed to its
  type at that moment. With no webhook registered it waits, and the missed deadline is logged as a
  REQ-04 error.
- At-least-once delivery, retried with backoff of 10 s doubling to 5 min, at most
  `WEBHOOK_MAX_ATTEMPTS` (10). Each success writes a `NOTIFIED` ledger entry with
  `within_max_delay`, so delivery timing is auditable via `/v1/events/{id}/audit`.
- The body is JSON with `notification_id` for deduplication, and is signed with
  `X-Signature-SHA256` (HMAC-SHA256 with the secret returned at registration, an addition to the
  `{webhook_id}` response).
- SSRF rules: http(s) only (https in production), no credentials in the URL, every resolved
  address must be public, connections are pinned to the vetted address, no redirects, no proxies.
  `WEBHOOK_ALLOW_PRIVATE_NETWORKS` relaxes the address rule outside production (local testing).
- Malformed or disallowed URLs get `400`. A failed verification ping gets `422` (Section 5).

## D-16: Test environment (Section 5.1 test routes, D-07)

- **Clock.** Simulated time = real time + an offset that only grows. The offset is stored in the
  database (`simulated_clock`) because the API and the worker are separate processes; each caches
  it for up to 1 s. `advance` takes a JSON integer `0 ≤ seconds ≤ 10 years`. `GET /v1/test/clock`
  (an addition) reads it.
- **Reset.** Truncates events, metrics, thresholds, webhooks, notifications and the ledger,
  clears `last_seen_at`, reseeds the baseline fixtures (app/modules/testing/fixtures.py) and sets
  the clock offset back to 0. Accounts and API keys are kept, so testers' keys keep working.
- **Fixtures.** Taken from the spec's examples (Section 5.2 system id, Model 5 loan-scorer and
  resume-ranker, AC-03 drift > 0.30) until the pod agrees a shared format (D-07).

## D-17: AC-01 cadence tolerance (REQ-01, AC-01, REQ-02)

**Gap.** AC-01 expects a sample every 5 min ± 10 s for an hour, checked from the timestamp log.
It doesn't say what the service does when a sample is more than 10 s off: nothing in Section 5
flags it, and a monitoring gap needs 3 missed intervals (D-11).

**Interim decision.** A sample more than 10 s off schedule is a *cadence violation* reported by
the AC-01 check over the `received_at` log (`tests/contract/cadence.py`), not by the service.
The boundary is inclusive: an interval of 290–310 s passes, anything outside fails. The service
stores the sample normally and opens no gap. Tests: `tests/test_ac01_cadence.py`,
`tests/integration/test_ac01_cadence_pg.py`, and the contract test
`test_ac01_one_hour_at_the_sampling_interval_opens_no_gap`.

**To agree.** Whether the service itself should flag late or early samples (an event or ledger
entry), and whether ±10 s should be a per-system setting like `gap_missed_intervals`.

## D-18: AC-05 checkpoints vs calendar months (REQ-05, AC-05, INV-10)

**Conflict.** AC-05 expects a record to be retrievable at 5 months 29 days and allows it to be
gone at 6 months 1 day. `retain_until` is detection + 6 calendar months, clamped to month end
(PostgreSQL's `interval '6 months'`). Counted the same way, 5mo29d can fall *after*
`retain_until` when the last month spans February. Example: detected 2026-09-27 20:00 gives
`retain_until` 2027-03-27 20:00, while +5mo29d and +6mo1d are both 2027-03-28 20:00.

**Today.** Nothing purges data (Phase 5), so the record is retrievable at every checkpoint.
`tests/integration/test_ac05_retention_pg.py` checks the literal 5mo29d, `retain_until` − 1 day
and `retain_until`, and accepts present or absent at 6mo1d. A purge that deletes exactly at
`retain_until` would therefore fail AC-05 for dates like the example.

**To agree.** Either read AC-05's "5mo29d" as "the day before `retain_until`" (the purge can
run at `retain_until`), or keep the literal reading and have the purge wait until at least
6mo1d.

## Other notes

- **Worker deployment.** Separate container from the same image (`python -m app.worker`, Q7.3).
  A session advisory lock keeps exactly one worker active; extra copies stand by.
- **Health endpoint.** `/healthz` reports `database` and `worker` (`ok` / `stale` / `missing`,
  from the heartbeat). Only an unreachable database returns `503`, because restarting the API
  container can't fix a stopped worker.
