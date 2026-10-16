# Spec decisions and open questions

The Phase 2 submission (pod specification of Sept 25, 2026, plus our team's Sections 7–9) is the
source of truth. This file records where its parts disagree or leave behavior open, and what this
codebase does about it. Items marked **pod** need agreement with the other team before they are
final, because cross-team tests depend on them.

| ID | Topic | Status |
|----|-------|--------|
| D-01 | Threshold direction (above/below) | Decided (team) |
| D-02 | `metric_type` column and `metric_name` naming | Decided (team) |
| D-03 | Event type/state names: Section 5 vs Models 1–2 | Interim, **pod** |
| D-04 | Duplicate metric submissions | Open, **pod**; temporary behavior |
| D-05 | Evidence bundle window (±1 h) vs flag-time hashing | Open, **pod** |
| D-06 | API details Section 5 leaves unspecified (Phase 3 endpoints) | Interim, **pod** |

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
checks those values. Phase 3 only creates `FLAGGED` events, so nothing past that is implemented yet.

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
  `uq_event_breach_per_sample` index.
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
| Pagination format | "paginated array of Event summaries" | `limit` (1–200, default 50) and `offset` (≥ 0) query params. Response `{"items": [...], "total", "limit", "offset"}` |
| List ordering | not specified | Newest `detected_at` first; `event_id` breaks ties so pages are stable |
| Event summary fields | not specified | `event_id, system_id, type, state, detected_at, metric_name` |
| Invalid list query (bad `state`, naive `since`, `limit=0`, ...) | only `401` listed | `400`, same as malformed bodies |
| Who may read events | only `401` listed | `viewer`, `reviewer`, `admin`. Monitored-system keys get `403` (they only submit metrics) |
| Who may submit metrics | system keys "are used for metric submission" | Only a system key whose `system_id` matches the body. Human keys get `403` |
| Malformed `event_id` in path | `404` if it doesn't exist | `404` (a malformed id can't exist) |
| `flagged_event_id` when no breach | "also contains" on breach | Omitted from the response |
| `value` | number | JSON number only. Strings, booleans, `NaN` and infinities get `400`. Stored as exact `numeric` |
| `timestamp` | ISO-8601 UTC | Must carry an offset (naive gets `400`). Other offsets are accepted and normalized to UTC; responses use `Z` |
| `system_id` | "UUID/string" in MetricSample, UUID in Event | UUID only (`400` otherwise) |
| Unknown `system_id` with a valid key | not specified | `400` (should not occur once auth exists) |
| `metric_type` differs from the configured threshold's | not specified | `400`, nothing stored. Silently mixing types could hide a breach |
| Unknown fields in the body | not specified | Ignored |
| Threshold used for a late sample | not specified | The version in force at the sample's `timestamp`, not at arrival |
| Viewing an event (`GET /v1/events/{id}`) | adds a view to the audit log | Writes a `VIEW` ledger entry in the same transaction; if the ledger write fails, the request fails (`501` until the ledger exists) |
| Listing events | "State Change: None" | No ledger entries |
| `deadline_utc`, `harm_fields`, `revision_history` | Event fields | Always `null` / `null` / `[]` until Phase 4 |

## Other notes

- **Worker deployment.** Q7.1 left open whether the worker runs as a separate container or as an
  in-process scheduler. Q7.3 describes separate API and worker containers built from one image.
  The worker owner decides; docker-compose has a placeholder comment for it.
- **Health endpoint.** Q7.3 specifies `/healthz` (database connectivity plus the worker heartbeat).
  The heartbeat check will be added together with the worker.
