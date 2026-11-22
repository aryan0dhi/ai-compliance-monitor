# Spec decisions and open questions

The Phase 2 submission (pod specification of Sept 25, 2026, plus our team's Sections 7–9) is the
source of truth. This file records where its parts disagree or leave behavior open, and what this
codebase does about it. Items marked **pod** need agreement with the other team before they are
final, because cross-team tests depend on them.

| ID | Topic | Status |
|----|-------|--------|
| D-01 | Threshold direction (above/below) | Decided (team); **pod** to confirm for shared fixtures |
| D-02 | `metric_type` column and `metric_name` naming | Decided (team); no API impact |
| D-03 | Event type/state names: Section 5 vs Models 1–2 | Decided (team, 2026-11-17): Section 5 names; **pod** to confirm |
| D-04 | Duplicate metric submissions | Open, **pod**; temporary behavior |
| D-05 | Evidence bundle window (±1 h) vs flag-time hashing | Open, **pod** |
| D-06 | API details Section 5 leaves unspecified (Phase 3 endpoints) | Interim, **pod** |
| D-07 | Configuration endpoints (thresholds, systems) are missing from Section 5 | Open, **pod** |
| D-08 | INV-8 (every read is logged) vs Section 5 (listing has no state change) | Open, **pod** + audit owner |
| D-09 | Repeated breaches: one event per breaching sample vs one per episode | Open, **pod** |
| D-10 | Timestamp plausibility (future/very old samples) | Open, **pod** |
| D-11 | Gap boundary: "longer than" 3 intervals vs Ex-3 detecting at exactly 3 | Decided (team, follows Ex-3); **pod** to confirm |
| D-12 | Monitoring-gap event states | Decided (team, 2026-11-17, with D-03); **pod** to confirm |
| D-13 | Roles for endpoints Section 5 doesn't assign (webhooks, test routes) | Interim, **pod** |
| D-14 | Ledger hash algorithm, and which reads are logged | Decided (team); **pod** to confirm for re-verification |
| D-15 | Webhook delivery semantics (fan-out, retries, signature, SSRF rules) | Interim, **pod** |
| D-16 | Test environment: shared clock, reset scope, fixtures | Interim, **pod** (with D-07) |
| D-17 | AC-01 cadence: what happens to a sample more than 10 s off schedule | Interim, **pod** |
| D-18 | AC-05 checkpoints: calendar 5mo29d can fall after the 6-month minimum | Open, **pod** |
| D-19 | Course testing interface: `/test/setup`, `/test/report` sessions, access, coverage | Interim (team) |
| D-20 | REQ-07 prefill: which harm fields a breach prefills, and with what evidence | Decided (team, 2026-11-17); **pod** to confirm |
| D-21 | What moves a breach from `FLAGGED` to `UNDER_REVIEW` | Decided (team, 2026-11-17); **pod** to confirm |
| D-22 | Disposition details B-08/B-09 leave open (blank rationale, history, re-confirmation, Ex-2) | Interim (team, #38); **pod** to confirm |
| D-23 | Deadline details B-10 leaves open (which tier is stricter, A after NOT_SERIOUS, same tier) | Interim (team, #39); **pod** to confirm |
| D-24 | Reminder details B-11 leaves open (catch-up, revisions, closing, payload) | Interim (team, #40); **pod** to confirm |
| D-25 | Report details B-12 leaves open (revision after assembly, submission, audit) | Interim (team, #41); document shape superseded by D-30 |
| D-26 | Which refused calls the audit trail records (Model 5 Ex-4) | Interim (team, #67); **pod** to confirm |
| D-27 | Scheduled ledger check and its alert (Model 5 Ex-5) | Interim (team, #71); **pod** to confirm the alert format |
| D-28 | REQ-16 erasure: what is personal data, when it is erased, how reads show it | Interim (team, #70, from Phase 2 Q7.1/Q7.2); **pod** to confirm the API field |
| D-29 | The other team's Phase 5 contract delta (`delta.md`, Δ1–Δ11, A1–A7) | Agreed by both teams; implemented except viewer writes |
| D-30 | The Δ1 report schema (`report.schema.json`, `pod-h-report-1`) and its PDF | Implemented (team) |
| D-31 | Tester coverage for Phase 5 (testing interface "Coverage fields", `coverage.model`) | Implemented (team); the design-requirement list is ours to confirm |

## Agenda for the next pod meeting

Ordered by impact on cross-team testing:

1. **D-06**: pagination shape, who may read events, `400` for invalid queries, and the id
   length/pattern bound (opaque ids). The other team's harness will call these endpoints
   directly.
2. **D-09**: one event per breaching sample (current) or one per breach episode. This changes
   how many events and notifications AC-03/AC-04/AC-16 should expect.
3. **D-04**: status codes for duplicate submissions (`200`/`409` now).
4. **D-07**: how thresholds are configured, and the shared fixture format for
   `/v1/test/reset`. Proposed: `docs/pod-interface/fixtures.json`, with the Model 5 scenarios
   (D-16).
5. **D-01**: shared fixtures must say whether each threshold is `above` or `below`.
6. **D-03** (with D-12): confirm the team decision of 2026-11-17 (Section 5 names, report states
   internal, gap states unchanged).
7. **D-08**: whether listing events writes ledger entries.
8. **D-21** (with D-12): a breach goes `UNDER_REVIEW` when its flag notification is first
   accepted, and stays `FLAGGED` with no webhook.
9. **D-05** and **D-10**: needed before Phase 5 retention and evidence hashing.
10. **D-11** to **D-16**: added with gap detection, notifications, auth, the ledger and the test
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

**Decision (team, 2026-11-17; pod to confirm).** The database and API keep the Section 5 names,
because the cross-team harness checks those values. No migration is needed.

- Breach events: `FLAGGED` → `UNDER_REVIEW` → `DISPOSITIONED` (serious tier) or `CLOSED` (not
  serious). Model 1's `CLASSIFIED(tier)` is `DISPOSITIONED` plus the event's tier;
  `CLOSED_NOT_SERIOUS` is `CLOSED`.
- `REPORT_READY` and `SUBMITTED` stay internal: the event remains `DISPOSITIONED`, and the report
  carries `assembled_at` and `submitted_at` instead (B-12, `POST .../report/submission`).
- Event types: `threshold_breach` and `monitoring_gap` (Model 2's `BREACH` / `GAP`).
- Monitoring-gap events keep their own states (D-12) and never take a breach state (INV-12).
- The critical-infrastructure tier is spelled `CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD`.

`tests/test_pod_interface.py` checks that `EventState` and `EventType` match the pod OpenAPI
enums, so any later change has to update both.

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

The page envelope and the error body are interim and codified in issue #25 (pod-interface B-22),
**not agreed**. Open: whether `total` is required; whether `400` keeps a list of `{loc, msg, type}`
(FastAPI's own shape, minus the input); a disallowed webhook URL as `400` or `422`.

| Topic | Section 5 says | Interim behavior |
|---|---|---|
| Pagination format | "paginated array of Event summaries" | `limit` (1–200, default 50) and `offset` (0 to 2⁶³−1, PostgreSQL's bigint limit) query params. Response `{"items": [...], "total", "limit", "offset"}` |
| List ordering | not specified | Newest `detected_at` first; `event_id` breaks ties so pages are stable |
| Event summary fields | not specified | `event_id, system_id, type, state, detected_at, metric_name` |
| Invalid list query (bad `state`, naive `since`, `limit=0`, ...) | only `401` listed | `400`, same as malformed bodies |
| Error body | not specified | `{"detail": <string>}` for every error status including `404` (unknown path), `405`, `500` (fixed text, traceback only in the log) and `501`; `400` validation errors list `{loc, msg, type}`. Never echoes input, URLs or exception text |
| Who may read events | only `401` listed | `viewer`, `reviewer`, `admin`. Monitored-system keys get `403` (they only submit metrics) |
| Who may submit metrics | system keys "are used for metric submission" | Only a system key whose `system_id` matches the body. Human keys get `403` |
| Malformed `event_id` in path | `404` if it doesn't exist | `404` (an id that is not an event's id can't exist) |
| `flagged_event_id` when no breach | "also contains" on breach | Omitted from the response |
| `value` | number | JSON number only. Strings, booleans, `NaN` and infinities get `400`. Stored as exact `numeric` |
| `timestamp` | ISO-8601 UTC | ISO-8601 string with an offset. Naive values and numbers (which a lenient parser would read as Unix time) get `400`. Other offsets are accepted and normalized to UTC; responses use `Z`. Values that fall outside year 1–9999 once converted to UTC get `400` |
| `metric_name` | string | 1–255 characters, no leading/trailing whitespace, no control characters (published as a `pattern` in the OpenAPI document). Whitespace variants would silently miss their threshold |
| `since` (GET /v1/events) | not specified | Compared with `detected_at` (inclusive). ISO-8601 with an offset only |
| Id format (`system_id`, `event_id`, `actor_id`, ...) | "UUID/string" in MetricSample, UUID in Event | Opaque string, 1-64 characters from `[A-Za-z0-9._~-]` (`400` otherwise). Internally UUIDs matched in canonical lowercase form only, so an uppercase or unhyphenated spelling of a real id is unknown (this changed from accepting any UUID spelling). A well-formed filter id that is no UUID gives an empty `200` on `GET /v1/events`; as a body `system_id` it is `403`. Proposed, awaiting pod agreement on the bound and pattern |
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

**Decision (team, 2026-11-17, with D-03; pod to confirm).** Gap events use Model 1's gap states. The database pairs states with types
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
  `WEBHOOK_ALLOW_PRIVATE_NETWORKS` relaxes the address rule outside production (local testing);
  `WEBHOOK_PRIVATE_HOSTS` relaxes it only for the named hosts (the test deployment's tester).
- Malformed or disallowed URLs get `400`. A failed verification ping gets `422` (Section 5).

## D-16: Test environment (Section 5.1 test routes, D-07)

- **Clock.** Simulated time = real time + an offset that only grows. The offset is stored in the
  database (`simulated_clock`) because the API and the worker are separate processes; each caches
  it for up to 1 s. `advance` takes a JSON integer `0 ≤ seconds ≤ 10 years`. `GET /v1/test/clock`
  (an addition) reads it.
- **Reset.** Truncates events, metrics, thresholds, webhooks, notifications and the ledger,
  clears `last_seen_at`, reseeds the fixtures and sets the clock offset back to 0. Systems not in
  the fixtures are deleted, so the fixture set is exact. Accounts and API keys are kept, so
  testers' keys keep working; a system with an account is kept too, without thresholds (open:
  delete such accounts and keys instead?).
- **Fixtures and scenarios.** `docs/pod-interface/fixtures.json` is the single copy: the app
  reseeds from it (`FIXTURES_PATH`), the contract suite reads it (`CONTRACT_FIXTURES`), and the
  README table is checked against it. Systems and thresholds come from the spec's examples
  (Section 5.2 system id, Model 5 loan-scorer and resume-ranker, AC-03 drift > 0.30). `scenarios`
  encodes Model 5 Ex-1 to Ex-5 as phase-gated steps (pod-interface B-24). The format is
  **proposed, not agreed**; the pod still needs to agree it (D-07), and that Ex-4's ledger check
  of denied attempts is Phase 4.

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

## D-19: Course testing interface (`POST /test/setup`, `GET /test/report`)

**Gap.** The course's cross-team testing interface requires both endpoints but leaves several
choices to each team. Spec: [docs/testing-interface/system-openapi.yaml](testing-interface/system-openapi.yaml).

**Interim decisions.**
- **Where.** At the service root, next to `/v1`, and only when test routes are enabled. They
  don't exist in production (404), like `/v1/test/*`.
- **Access.** No API key. The tester calls them knowing only the base URL, so a key would break
  the interface. This is acceptable because setup only resets the test environment; the `/v1`
  project API still needs keys, which are sent privately and never put in URLs or the registry.
- **One session at a time.** Setup resets the one shared test database and coverage is measured
  per process, so sessions can't overlap. A new setup with a new id ends the active session and
  freezes its report; that is also the cleanup after an interrupted run. A setup while another
  is still in progress gets `409`; a reused id gets `409`.
- **Setup data.** `setup` must be a JSON object but doesn't select fixtures yet (D-07). Every
  session starts from the baseline fixtures (as `POST /v1/test/reset`) with the test clock at
  real time. API keys survive.
- **Failure.** If the reset fails (for example, the database is down), setup returns `500` with
  `source: "system"` and the session is forgotten, so the same id can be set up again.
- **Coverage.** Line coverage of `app/` in the API process since the session's setup, measured
  in-process with coverage.py's `sys.monitoring` core (it also sees request threads that already
  exist). The worker process and the testing interface's own modules aren't measured. When
  measurement isn't possible (coverage.py not installed, or the process already runs under
  `coverage run`), `covered` and `total` are `null` and `basis` says why.
- **Lifetime.** Sessions are in memory. After an API restart their reports are `404`, and the
  tester sets up again. The last 100 sessions are kept.

## D-20: Harm-field prefill (REQ-07, AC-07, B-07, Model 5 Ex-1)

**Gap.** B-07 says each of the four Art. 3(49) fields gets `prefill_evidence` "drawn from the event
record, or `null` when there is none (never guessed)". Model 5 Ex-1 expects a loan-scorer accuracy
breach to prefill `fundamental_rights` and leave the other three blank, but nothing in a breach
record (metric, value, threshold) says which consequence it concerns.

**Decision (team, 2026-11-17; pod to confirm).** Which fields are prefilled is **configuration of
the monitored system**, never inferred from the metric:

- `monitored_system.harm_fields` lists the fields the system's use can affect (empty by default).
  Set with `python -m app.cli create-system --harm-field <name>` (repeatable), and per system in
  `docs/pod-interface/fixtures.json` (`"harm_fields"`, optional). loan-scorer lists
  `fundamental_rights` (Annex III credit scoring); resume-ranker lists none, as no scenario
  checks it.
- On a breach, each listed field's `prefill_evidence` is built from the event's own breach facts
  (metric, observed value, threshold, time). Every other field is `null`. All four start
  `PENDING`; the reviewer still confirms each one (REQ-07, REQ-08a).

## D-21: When a breach goes under review (Model 1, Model 5 Ex-1/Ex-4)

**Gap.** Section 5 and Model 1 list `UNDER_REVIEW` after `FLAGGED` but don't say what triggers
it. Model 5 expects it to have happened by Ex-1 `under-review` (PT6M, after the flag notification
reached the reviewer) and Ex-4 `premature-report` (PT1H, although no reviewer has opened the
event).

**Decision (team, 2026-11-17; pod to confirm).** A breach moves `FLAGGED` → `UNDER_REVIEW` when a
webhook **first accepts** (any `2xx`) its `flag_notification`, the same moment a gap moves
`GAP_OPEN` → `REVIEWER_NOTIFIED` (D-12):

- The change and its `NOTIFIED` ledger entry (with `"state": "UNDER_REVIEW"` in the payload) are
  written in one transaction. Later deliveries (other webhooks, retries, reminders) add `NOTIFIED`
  entries without a `state` and never move the event again.
- Viewing the event doesn't change its state; nothing else moves it to `UNDER_REVIEW`.
- **No webhook registered:** the notification stays undispatched (D-15) and the breach stays
  `FLAGGED` until a subscribed webhook accepts it. Failed attempts don't change the state.
- A breach that is already `DISPOSITIONED` or `CLOSED` when the notification is accepted keeps
  its state.

A harness reading the state right after a breach may see either `FLAGGED` or `UNDER_REVIEW`,
because the worker can deliver within one pass; `tests/contract` accepts both there.

## D-22: Disposition details (REQ-07, REQ-08a/b, REQ-09, B-08, B-09, Model 5 Ex-2)

**Gaps.** B-08 and B-09 define the disposition rules but leave some behavior open, and Model 5
Ex-2 disagrees with B-08.

**Interim behavior (#38; pod to confirm):**

- **Ex-2 vs B-08.** B-08 accepts a disposition only when all four harm fields are `CONFIRMED` or
  `NOT_APPLICABLE`, including `NOT_SERIOUS`. Ex-2 assigns `NOT_SERIOUS` without confirming any
  field, which B-08 refuses with `409`. We follow B-08 and added a `confirm-fields` step (all four
  `NOT_APPLICABLE`) to Ex-2 in `fixtures.json`.
- **Confirmations.** The interface has no separate call to confirm fields, so they are sent in
  the disposition body (`harm_fields`). A refused call stores none of them. A later call may
  confirm a field again: the row keeps the latest confirmation and the ledger keeps all of them.
  The contract suite's `confirm_fields` step adds its confirmations to the next disposition call.
- **Blank rationale.** A `NOT_SERIOUS` rationale that is empty or only whitespace is `400`. The
  pod schema's `minLength: 1` would accept whitespace.
- **History.** `revision_history` lists every disposition, oldest first, including the first
  (`previous_tier = null`). The last entry is the current tier. Assigning the same tier again is
  still a revision.
- **Roles.** Only reviewer keys may dispose (B-13); admin keys get `403` like viewers. The role is
  checked before the event id, so a viewer gets `403` even for an unknown event.
- **Status codes.** `401`, then `400` (body), then `403` (role), `404` (event), `409` (pending
  field or monitoring gap).
- **Audit.** One `DISPOSITION` ledger entry per accepted call (B-14), with the tier, previous tier,
  rationale, new state and the confirmations made in that call. A refused call changes nothing;
  since #67 a `403`/`409` on an existing event adds one `REFUSED` entry (D-26).
- **Not in #38.** Deadlines and the rule for revising to a less strict tier (B-10): D-23, #39.
  Reminders (B-11): #40.

## D-23: Deadline details (REQ-10, REQ-15, B-10, B-15, AC-11, AC-17)

**Gaps.** B-10 gives `deadline_utc = A + days(tier)` and the stricter/looser revision rules but
doesn't say how tiers are ordered, what happens around `NOT_SERIOUS`, or what a repeat of the same
tier does. Its looser-revision sentence ("keeps the current deadline unless a rationale is given
(otherwise `409`)") reads both ways; `DispositionIn` says a rationale is required.

**Interim behavior (#39; pod to confirm):**

- **Order.** Stricter means fewer days: `CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD` (2) before
  `DEATH` (10) before `OTHER_SERIOUS` (15) before `NOT_SERIOUS` (no deadline).
- **A** is the time of the first **serious** disposition and never moves. An event first
  assessed `NOT_SERIOUS` and later revised to a serious tier is anchored at that revision.
- **Stricter:** `min(current deadline, A + days(new tier))`; with no current deadline (after
  `NOT_SERIOUS`), `A + days(new tier)`.
- **Less strict:** needs a non-blank rationale, else `409` and nothing changes; with one the
  deadline becomes `A + days(new tier)`. Revising to `NOT_SERIOUS` clears it (it always has a
  rationale, B-08).
- **Same tier again:** keeps the current deadline.
- **Storage.** Each disposition stores the deadline it set (`revision_history[].deadline_utc`),
  so a later rule change never rewrites past deadlines. Days are exactly 24 h on the service
  clock, so `/v1/test/clock/advance` applies.
- **B-15.** `detected_at` is set at flagging and nothing in review changes it.
- **Contract suite.** Phase 4 scenario steps advance the service clock to their `at` offset
  (test routes needed) and check deadlines against it, allowing 60 s for real time spent.

## D-24: Reminder details (REQ-11, B-11, AC-12)

**Gaps.** B-11 says each `reminder` webhook gets exactly one delivery at 50 %, 80 % and 100 % of
the window from `A` to `deadline_utc`. It doesn't say what happens when the deadline moves, when
the event closes, or when several checkpoints pass at once.

**Interim behavior (#40; pod to confirm):**

- **Who.** Events in `DISPOSITIONED` (a serious tier). `CLOSED` events get none, including
  later checkpoints of an event revised to `NOT_SERIOUS`.
- **When.** The worker checks on every pass (seconds apart). A checkpoint is due when the service
  clock reaches `A + p % × (deadline − A)`, to the microsecond. Every due checkpoint is queued in
  order, so a worker that was down or a clock advanced past several checkpoints catches up.
- **Revisions.** Checkpoints still to come follow the current deadline (and the same `A`). One
  that was already queued is never sent again (`uq_notification_reminder_once`), even if the
  revision puts it later. A stricter tier whose deadline has already passed sends the remaining
  checkpoints at once.
- **Delivery.** Through the outbox like flag notifications: one delivery per subscribed webhook,
  at least once on the wire; receivers deduplicate on `notification_id`. The body carries
  `reminder: {elapsed_percent, deadline_utc}` (pod `WebhookDelivery`), and `event` is the event
  snapshot with `deadline_utc`. A delivered reminder adds a `NOTIFIED` ledger entry; queuing it
  adds none, because it doesn't change the event.

## D-25: Report details (REQ-08a, REQ-12, REQ-13, B-12, AC-13)

**Gaps.** B-12 says the report is `409` until a serious tier is assigned and that repeated calls
return the same document. It doesn't say what a revision after assembly does, what
`markReportSubmitted` (**Proposed**) checks, or what goes in the audit trail. The contract marks
both endpoints Phase 5; they are implemented now because they follow from #38.

**Interim behavior (#41; pod to confirm):**

- **When.** A report exists while the event's *current* (latest) disposition is a serious tier.
  Before any disposition, after `NOT_SERIOUS` (including a revision to it), and for monitoring
  gaps (INV-12) both endpoints are `409`.
- **Assembly.** The first `GET` for the current determination assembles the report: the event as
  it stands (state, harm fields, `revision_history`), the four harm fields, the determination
  (tier, reviewer, rationale of the latest disposition), `detected_at`, `determination_at` (that
  disposition's `recorded_at`), `assembled_at` (service clock) and `deadline_utc`. The JSON text
  is stored and every later call returns it byte for byte, followed by `report_sha256` (sha256 of
  the stored text) and `submitted_at`. **Superseded by D-30:** the document is now
  `pod-h-report-1`, served with nothing appended.
- **Revision after assembly.** The report belongs to the determination it was assembled from. A
  revision gets its own report on its next request, with a new `assembled_at` and hash; the
  earlier report is kept (insert-only, `report` table) but no longer served. Neither D-03 state
  (`REPORT_READY`, `SUBMITTED`) is visible: the event stays `DISPOSITIONED`.
- **Submission.** Reviewer keys only; optional `note` (at most 2000 characters, else `400`).
  The current report must already be assembled (`GET` first) and not yet marked, else `409`. It
  sets `submitted_at` once and returns the report; the stored document itself doesn't change. A
  revision's new report starts unsubmitted.
- **Roles.** Reviewer keys only (B-13); viewers, admins and system keys get `403`, checked before
  the event id.
- **Audit (B-14).** One ledger entry per successful call: `REPORT_ASSEMBLED` (with the hash) for
  the call that assembles, `VIEW` for each later read, `REPORT_SUBMITTED` (with the note) for the
  submission. A refused call changes nothing; since #67 a `403`/`409` on an existing event adds
  one `REFUSED` entry (D-26).
- **Concurrency.** The event row is locked while a report is read or changed, as for
  dispositions, so concurrent first requests assemble one report.

## D-26: Refused attempts in the audit trail (REQ-13, REQ-14, B-14, Model 5 Ex-4)

**Gap.** B-14 asks for one audit entry per read and per change. A refused call is neither, but
Model 5 Ex-4 expects both `viewer-tier` (a viewer's disposition, `403`) and `premature-report`
(a report before any disposition, `409`) in the ledger (`ledger_records_attempt`). Neither B-14
nor Ex-4 says which other refusals count.

**Interim behavior (#67; pod to confirm):**

- **What is recorded.** A `403` or `409` from `POST /v1/events/{id}/disposition`,
  `GET /v1/events/{id}/report` or `POST /v1/events/{id}/report/submission` adds exactly one
  `REFUSED` entry for that event. It gives the caller's `actor_id` and a payload of `event_id`,
  `operation` (the operationId), `status`, `role` and `reason`. It shows in
  `GET /v1/events/{id}/audit` as `action = REFUSED`.
- **What isn't.**
  - Unknown or malformed ids (`404`, or a viewer's `403` on an id that names no event): there's
    no event to attach an entry to, and anyone could grow the ledger with made-up ids.
  - `400` bodies.
  - Refusals on other endpoints. Ex-4 `viewer-denied` expects `POST /v1/webhooks` and
    `POST /v1/metrics` to add nothing.
- **How.** The refused call's own transaction rolls back as before, so the event, its fields and
  its dispositions are untouched. The entry is appended afterwards, in its own transaction.
  Recording is best effort: if it fails, the caller still gets the `403`/`409` and the failure is
  logged.
- **Volume.** Each recorded refusal needs a valid key and an existing event id, so repeated
  attempts are attributable. Rate limiting is still open (HANDOFF §9).

With this, `/v1/meta` lists phase 4. Ex-4 `premature-report` also expects `UNDER_REVIEW`, which
needs a webhook to have accepted the flag notification (D-21). The live contract run has one.

## D-27: Scheduled ledger check and alert (REQ-06, Model 5 Ex-5, D-14)

**Gap.** Ex-5 `nightly-alert` expects a `critical` alert after tampering, but neither B-06 nor
the pod interface says how often the ledger is checked, how a service alerts, or who receives
it. D-14 also left open how truncation (entries removed from the end) is detected.

**Interim behavior (#71; pod to confirm):**

- **When.** The worker checks once per `LEDGER_CHECK_INTERVAL_S` of service clock (default
  86400 s, "nightly"), and at worker start if there is no earlier check. On the test deployment
  `/v1/test/clock/advance` brings it forward.
- **What.**
  - It re-walks the whole chain, exactly as `GET /v1/ledger/verify` does.
  - The head of the last passing check (`head_seq`, `head_hash`) must still be in the chain
    with the same hash. Entries removed or replaced at the end are therefore detected, which the
    chain alone can't show.
  - Each result is a `ledger_check` row; the check writes nothing to the ledger itself.
- **Alert.**
  - The first failing check after a passing one (one per incident) queues a `ledger_alert`
    notification and logs at CRITICAL.
  - The notification goes to every webhook subscribed to `ledger_alert`. Registering webhooks
    is admin-only (D-13).
  - The body is `{notification_id, notification_type, created_at, deliver_by, alert}`, where
    `alert` has `severity: "critical"`, `checked_at`, `problem`, `first_mismatch`, the verified
    head and the last passing head.
  - Delivery uses the outbox (retries, signatures), and adds a `NOTIFIED` ledger entry with no
    event. A later passing check closes the incident, and the next failure alerts again.
- **Pod interface (Proposed).** `ledger_alert` added to `NotificationType`, `WebhookIn`
  `maxItems` 3, and the new `LedgerAlertDelivery` schema.
- **Limits.** `ledger_check` is in the same database as `ledger_entry`, so someone who can
  rewrite both can hide tampering. Shipping each head to an outside store (or to the alert
  webhook every night) would close that.
- **Contract suite.** A black-box test can't tamper with another team's database, so
  `await_alert` stays deferred; PostgreSQL integration tests cover it with direct SQL edits.

## D-28: Erasing personal data at the end of retention (REQ-16, AC-18, REQ-05, D-18)

**Gap.** REQ-16 asks that a record's personal-data fields can be made permanently unreadable after
its retention period without changing its place in the tamper-evident chain. Section 5 has no
field or operation for it, and nothing says which fields are personal.

**Design (team, Phase 2 Q7.2 Decision 1 and Q7.1).** Per-event keys, as planned in Phase 2:
- **Personal data** is free text a person writes about an event: the disposition `rationale`, the
  `evidence` a reviewer gives when confirming a harm field, the report `submission_note`, and the
  assembled report document (it quotes the rationale). Machine-generated values (metric values,
  prefilled breach evidence, ids, timestamps) are not. Actor ids stay readable so the audit trail
  works; they are pseudonymous.
- Each such value is sealed with the event's AES-256-GCM data key before it is stored or put in a
  ledger payload. The data key is stored only wrapped by `RECORD_KEY_MASTER_KEY` (`record_key`).
- **When.** The worker erases an event's personal data once the service clock reaches
  `max(retain_until, detected_at + 6 calendar months + 1 day)`. The extra day keeps AC-05's
  literal 5mo29d check intact whichever way D-18 is decided. The test clock drives it.
- **How.** The key is destroyed (`wrapped_key` NULL, `destroyed_at` set) with one
  `PERSONAL_DATA_ERASED` ledger entry (actor null). No row or earlier ledger entry changes, so
  `GET /v1/ledger/verify` still passes and an earlier export still re-verifies (AC-18).
- **Reads after erasure.** Rationales read as `null`; `GET /v1/events/{id}` adds
  `personal_data_erased_at` (**Proposed** for the pod interface; additive and optional, not yet
  in `docs/pod-interface/openapi.yaml`). The report and a new disposition are `409` (the document
  quotes personal data). The event and its metric data stay (REQ-05 is a minimum, not a purge).
- **Limits.** Ledger entries written before migration 0012 keep the plaintext they were hashed
  with; reset the test environment after deploying 0012. Without `RECORD_KEY_MASTER_KEY` the
  public development key is used outside production, which protects nothing.

**To agree with the pod.** The `personal_data_erased_at` field and `409` for an erased report, and
whether deleting rows after retention (#69) is wanted at all once personal data is erased.

## D-29: The Phase 5 contract delta (Δ1–Δ11, A1–A7)

**Source.** The other team's `delta.md` (status "proposed"). Our team accepted it on 2026-11-22,
including its three open inputs. Per the delta, every row needs both teams' agreement and a
**Change** row also needs the instructor's sign-off. We don't have the shared contract text the
delta modifies (its `report.schema.json` came later: D-30), so where the delta is silent we
keep our own behaviour and record it here.

**Error codes (Δ7), implemented.**
- Errors the delta names carry `error_code` and `message` **next to** the existing `detail`
  (B-22), so clients reading either shape keep working. `message` repeats the text.
- Coded today: every `400` (`MALFORMED_QUERY` when a query parameter is invalid, otherwise
  `MALFORMED_BODY`), and `409 ALREADY_ERASED` for a disposition on an erased event (A7).
  Later rows add the report, submission and erasure codes.
- Not coded: errors the delta names no code for (`401`, `403`, `404`, other `409`s). The delta
  says the baseline contract codes every error, but we don't have the baseline's code list, so
  we don't invent names.
- The contract suite accepts both our body and the delta's `{error_code, message}`.

**A7, implemented.** A disposition on an erased event is `409 ALREADY_ERASED` ("the personal
data of this event has been erased"). It is checked after the role (`403`), the body (`400`) and
the event (`404`), and before the monitoring-gap check (the delta's `EVENT_NOT_REVIEWABLE`).

**Δ6 and A4 (erasure on request, `RECORD_ERASED`), implemented.**
- `POST /v1/events/{event_id}/erasure`: reviewer only (`403` otherwise); body empty or `{}`
  (else `400 MALFORMED_BODY`); `404` unknown event; `409 RETENTION_NOT_EXPIRED` before
  `retain_until`; `409 ALREADY_ERASED`. `200 {event_id, erased_at, erased_fields}`.
- `erased_fields` (our reading; the delta gives no list) names those of `disposition.rationale`,
  `harm_fields.evidence`, `report.document` and `report.submission_note` that held anything.
- An event with no personal data yet gets an already-destroyed key, so it is erased too.
- Both this and the end-of-retention job (D-28, which still waits until
  `max(retain_until, detected_at + 6 months + 1 day)`) write one `RECORD_ERASED` ledger entry
  with `trigger` (`MANUAL` or `RETENTION`) and `erased_fields`. Migration 0014 allows the type;
  `PERSONAL_DATA_ERASED` stays valid for older entries.
- Refused erasure calls are not ledgered (D-26 covers disposition and report calls only).

**Δ8 (`WEBHOOK_REGISTERED`), implemented.** `POST /v1/webhooks` now appends one
`WEBHOOK_REGISTERED` ledger entry (actor: the admin; no event) with payload
`{webhook_id, url_sha256, event_types}`, where `url_sha256` is the lowercase hex SHA-256 of the
UTF-8 URL. The URL itself never enters the ledger. The delta changes `url` to `url_sha256` in an
entry our ledger didn't have before, so for us the whole entry is new; the other payload fields
are our choice (migration 0015).

**Δ5 (verification runs), implemented.**
- `GET /v1/ledger/verifications`: every run, scheduled or manual, newest first, as a bare
  array. Readers: viewer, reviewer and (our D-13 reader set) admin; system keys `403`.
- `POST /v1/ledger/verifications`: reviewer only. Verifies the whole chain now, exactly like the
  daily check (#71), and returns `201` with the run, `trigger: MANUAL`. A failing run alerts as
  a scheduled one does (D-27). It counts as the latest check, so the next scheduled run is a day
  after it.
- A run (our field names; the delta names only `trigger`, and A1 names `finished_at`, `valid`,
  `head_check`): `{verification_id, trigger, finished_at, valid, entries_checked,
  first_mismatch, head_seq, head_hash, head_check, problem}`.
- `head_check` (our reading): `MATCH`, `MISMATCH` (entries removed from or replaced at the end
  since the last passing run) or `NO_ANCHOR` (no earlier passing run); null for runs before
  migration 0016.

**A1 (`/healthz` fields), implemented.** The body adds `version` (`APP_VERSION`, null when
unset), `last_verification` (`{finished_at, valid, head_check}` of the newest run of any
trigger, or null), `last_verification_age_seconds` (whole wall-clock seconds, never negative,
since the run time is on the service clock) and `reset_count` (0 before the first reset). The
`503` body has the same keys: `version` from the environment, the other three null. Ledger
integrity never makes `/healthz` 503. `reset_counter` (migration 0016) holds the count, outside
the tables a reset truncates; each reset adds one.

**405 `Allow` header.** A path with separate GET and POST routes (`/v1/ledger/verifications`)
now lists both in `Allow`, taken from the API's own OpenAPI document.

**Δ3 and Δ4 (submissions and report versions), implemented.**
- Each stored report version has a `report_id` (migration 0017; existing reports get one).
- `GET /v1/events/{event_id}/reports` (reviewer or viewer; admin and system keys `403`): a
  bare array, oldest first, of `{report_id, event_id, report_version, status, assembled_at,
  report_sha256, submitted_at}`. `status`: `ASSEMBLED` / `SUBMITTED` for the version of the
  current serious-tier disposition, `SUPERSEDED` / `SUBMITTED_SUPERSEDED` for the others.
  `report_version` is the disposition number it was assembled from (our reading).
- `GET /v1/reports/{report_id}` (reviewer only): that version, byte for byte as assembled,
  with one `VIEW` entry; `404` unknown; `410 RECORD_ERASED` after erasure.
- `POST /v1/events/{event_id}/report/submission` now takes the delta's body
  `{report_id, json_sha256, report_type, expected_next_report_date?, authority_reference?}`
  and returns `201` with the submission object. The Phase 4 `{note}` body is gone (`400`).
  `json_sha256` must equal the version's `report_sha256`. The event's first submission is
  `INITIAL`, later ones `FOLLOW_UP`. One submission per version.
- Our reading of the error order: `403`; `404` (event, or a `report_id` that isn't one of the
  event's versions); `410 RECORD_ERASED`; `409 NOT_SUBMITTABLE` (no serious-tier
  determination now); `REPORT_NOT_CURRENT`; `REPORT_HASH_MISMATCH`; `REPORT_ALREADY_SUBMITTED`.
- `authority_reference` is not treated as personal data (D-28), so it isn't sealed.
- `GET /v1/events/{event_id}/report` itself changed with the Change rows below (#83).

**Change rows (Δ2, Δ9, A2, A3, A5, A6): implemented (#83).** Both teams agreed. Our team
read the Phase 5 instructions as requiring instructor sign-off only for changes to the shared
project scope; #83 was checked to leave every Phase 2 requirement and acceptance criterion
unchanged (scope check on the PR) and merged on that basis.
- Δ2 `GET /v1/events/{event_id}/report`: `409 NO_DISPOSITION` before any disposition;
  `410 RECORD_ERASED` after erasure (was `409`); `ETag`, `X-Report-Id`, `X-Report-Version`
  (the disposition number) and `X-Report-Sha256` on `200` (also on `GET /v1/reports/{id}`);
  `format=json` (default). `format=pdf` returns the PDF of the same report (D-30).
  A current tier of NOT_SERIOUS stays an uncoded `409` (Phase 2 Model 5 Ex-2; D-30).
- Δ9 / A2: `POST /v1/test/clock/advance` is `400` if it would move the simulated time to or past
  `9999-07-01T00:00:00Z`; `POST /v1/metrics` is `400 MALFORMED_BODY` for a `timestamp` at or after
  it.
- A3: sealed values are `pd1:` followed by the sealed value (was `enc:v1:`, which still opens).
  Our ledger entry is `DISPOSITION`, not the delta's `DISPOSITION_RECORDED`/`_REVISED` (we don't
  have the baseline's names).
- A5: each harm field on `GET /v1/events/{id}` has `evidence` (the reviewer's text, sealed in
  storage); after erasure it, like every rationale, is `null` and the event is still `200`.
- A6: reset already empties every Phase 5 table and keeps `reset_count` + 1; the deadlock retry
  (`500` after three) is not built: our reset is one transaction.


**Open input "viewer writes", not implemented.** The delta notes that viewers may call
`POST /v1/webhooks` and the test endpoints. Here they stay admin-only (D-13): Phase 2 §5.1 says
viewers "can read events/audit/ledger but not write", and opening them would let a viewer send
event data to any URL, wipe the shared environment, or advance the clock past retention and
trigger irreversible erasure (REQ-16). Changing it needs instructor sign-off.

## Other notes

- **Worker deployment.** Separate container from the same image (`python -m app.worker`, Q7.3).
  A session advisory lock keeps exactly one worker active; extra copies stand by.
- **Health endpoint.** `/healthz` reports `database` and `worker` (`ok` / `stale` / `missing`,
  from the heartbeat). Only an unreachable database returns `503`, because restarting the API
  container can't fix a stopped worker.

## D-30: The Δ1 report schema (REQ-12, AC-13, contract delta Δ1)

**Source.** The other team's `report.schema.json` (`pod-h-report-1`, JSON Schema 2020-12), received
after D-29. Every report `GET /v1/events/{event_id}/report` assembles now validates against it.
This section records our choices where the schema leaves one, and what is still missing.

**Document (`app/modules/review/report_document.py`).**
- Served exactly as stored at assembly. The D-25 `report_sha256` and `submitted_at` are no longer
  appended, because the schema forbids extra keys. The hash is `X-Report-Sha256`, which is now
  the SHA-256 of the body (it is also Δ3's `json_sha256`). Submission status is in the version
  list (Δ4). Reports stored before this change keep their D-25 shape and are served as stored.
- `report_kind` is always `SERIOUS_INCIDENT` and `submission_marking` is null. A NOT_SERIOUS
  event still has no report (`409`), as in Phase 2 Model 5 Ex-2 and REQ-08a. The schema's
  `NOT_SERIOUS_CLOSURE` branch is unused. Δ1 names that kind, but adopting it would change a
  Phase 2 acceptance example, which is shared scope.
- Ids: `report_id` is `rpt-` + the report UUID's hex, and `disposition_id` is
  `dsp-<event hex>-<seq>`, both derived from stored keys (no migration). Inputs accept the `rpt-`
  form and the UUID form served before.
- `event`: the schema's nine fields. `evidence_bundle` is the Model 2 ±1 h window around the
  triggering sample, ending at assembly if that is earlier (never before the breach). It holds
  the same system and metric's samples, bounds included, and `evidence_sha256` is over its
  canonical JSON. `event.evidence_bundle_hash` itself stays null (D-05).
- `harm_fields`: the schema's names (`health_safety_harm`, `critical_infrastructure_disruption`,
  `fundamental_rights_infringement`, `property_environmental_damage`) inside reports only.
  Disposition requests and event responses keep the Phase 2 names.
- `determination`:
  - `assigned_at` is the current disposition's time;
  - `tier_label` is a plain-English tier name with its Article 73 paragraph;
  - `calendar` describes `deadlines.py` (UTC days of exactly 24 h; REQ-10's configurable
    calendar isn't built);
  - `revisions` lists every disposition after the first.
- `timestamps.determined_at` is A, the first serious-tier disposition (the deadline's anchor,
  D-23). `last_revised_at` is the current disposition's time after a revision, otherwise null.
  `risk_indicator.detection_to_assignment_seconds` is A − `detected_at` in whole seconds
  (REQ-15, AC-17).
- `regulation.reporting_window_days`: 2, 10 or 15 by tier (`DEADLINE_DAYS`).
- `ledger_anchor`: the newest ledger entry when the report was assembled. Its own
  `REPORT_ASSEMBLED` entry, which carries the report's hash, comes after it. On an empty ledger
  (only in unit tests) it is seq 0 and the genesis hash.

**PDF (`format=pdf`; `app/modules/review/report_pdf.py`).**
- **Content.** The PDF is rendered from the stored JSON text, the same bytes `format=json`
  serves:
  - every value is printed under its JSON path, in document order, with nothing added or left
    out (null/true/false as JSON);
  - the exact JSON is embedded as the attachment `report.json`;
  - each page's footer gives the report id and the JSON's SHA-256.
- **Library and font.** fpdf2 2.9.0 (with Pillow and fontTools, pinned with hashes). The text
  is set in embedded DejaVu Sans 2.35 (Bitstream Vera licence, `app/assets/fonts`).
- **Unicode.** Characters the font has glyphs for (Latin, Greek, Cyrillic, symbols) print as
  themselves. Any other character, for example CJK, prints as `[U+XXXX]`, never as a blank or a
  look-alike, and the attachment keeps it exactly. Tabs and line breaks print as spaces.
- **Same bytes every time.** The PDF's dates are the report's `assembled_at` and its file id
  comes from the JSON's hash. With the font and library pinned, the same stored report gives the
  same PDF on every request, which is how we read Δ2's "bytes … stored at assembly" without
  storing a second copy.
- **No separate copy.** Not storing the PDF means no second copy of the personal data to seal
  and erase (REQ-16): once the event's key is destroyed, both formats are `410`.
- **Headers.** `X-Report-Id`, `X-Report-Version` and `X-Report-Sha256` identify the report, as
  for JSON (the SHA-256 is the JSON's). The `ETag` is the PDF's own SHA-256.
  `Content-Disposition: inline; filename="rpt-….pdf"`.
- **Refusals.** Requests, roles, refusals and ledger entries are exactly those of `format=json`.
- Reports stored before this section (D-25 shape) render too: their fields are printed the same
  way.

- The schema is copied unchanged to [`pod-interface/report.schema.json`](pod-interface/report.schema.json).
  `tests/test_report_schema.py` validates served reports against it, plus the ordering
  `detected_at < determined_at < assembled_at` that JSON Schema can't express. Those tests are
  strict expected failures until the report follows the schema.
- **Article 73 template (`article73_template`).** The field list is the Commission's own template,
  "Incident Report for Serious Incidents under the AI Act (High-risk AI systems)", version 1.0.0,
  SB-10407, published with the draft Article 73 guidance (consultation of 26 September 2025;
  [PDF](https://ec.europa.eu/newsroom/dae/redirection/document/119623)). Every section, group
  and field is listed in template order under the template's own numbering
  (`app/modules/review/article73.py`). The service fills only what it holds:
  - 1.2.c detection time: `mapped`;
  - 1.2.d awareness date (the first serious determination): `derivable`;
  - 1.2.g classification: `derivable`, from the tier and the CONFIRMED harm fields, when that's
    unambiguous;
  - 2.2.a system name: `derivable`.

  Contact, address and product details are `not_available`. Everything that needs judgement is
  `requires_reviewer_input`. 4.2.b (the not-reportable rationale) is `not_available`, because
  NOT_SERIOUS events have no report here.

## D-31: Tester coverage for Phase 5 (course testing interface, "Coverage fields")

**Rule.** The testing interface asks every suite report for `code`, `component` and `system`
coverage. From Phase 5 it also asks for `model`, but only when the tester tests its own team's
system. Coverage is about what the suite *evaluated*; `results` say whether the system behaved.

**Before.** Component and system coverage counted only tags of **passing** tests, against the
Phase 3 lists (9 endpoints, 6 ACs). There was no `model` entry.

**Now** (`tester/main.py`, `tests/contract/scope.py`):
- `component`: the 17 pod-interface operations up to Phase 5 (`docs/pod-interface/openapi.yaml`;
  a test keeps the two in step).
- `system`: AC-01 to AC-18. Phase 2 §6.4 puts all of them in scope by Phase 5.
- `model`: our 18 design requirements from the Phase 4 design: 8 Model 1 states, the 5 Model 4
  invariants the design marks API-checkable, and the 5 Model 5 examples. The mapping to tests is
  [`docs/testing-interface/model-coverage.md`](testing-interface/model-coverage.md). It is
  included when the target is in `TESTER_SELF_URLS`, and omitted otherwise.
- An item counts once when a test that **passed or failed** evaluated it.
- A Model 5 scenario reports what its executed steps evaluated (`record_property("covers")`): the
  ACs of each step (`STEP_ACS`), the operations it called, and the states it checked. The example
  itself counts only when no step was deferred. Static `@covers` tags apply to every other test.

**To confirm.**
- The design-requirement list is the team's choice. It is one place to edit (`DESIGN_REQUIREMENTS`
  in `scope.py`) if the team lists more (for example all thirteen Model 4 invariants) for this
  milestone.
- Δ5 and Δ6 (verification runs, erasure) are agreed but not yet in the pod interface's
  `openapi.yaml`, so they aren't components here.

