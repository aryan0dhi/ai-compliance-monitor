# Database schema

PostgreSQL schema for the monitoring service, based on **Model 2** of the Phase 2 specification
and the **Section 5.2** API schemas. Where this schema differs from Model 2, the reason is linked
to [spec-decisions.md](spec-decisions.md).

| Migration | Tables | Requirements |
|---|---|---|
| `0001` | (baseline, empty) | |
| `0002` | `monitored_system`, `metric_sample`, `threshold_version`, `event` | REQ-01, REQ-03, REQ-05 (storage only) |
| `0003` | `app_user`, `api_key`, `ledger_entry`, `webhook`, `notification`, `notification_delivery`, `simulated_clock`, `worker_heartbeat`; gap columns/states on `event`; `gap_missed_intervals` on `monitored_system` | REQ-02, REQ-04, REQ-06, REQ-13, REQ-14 |
| `0004` | `harm_fields` on `monitored_system` | REQ-07 (D-20) |
| `0005` | `harm_field` | REQ-07 (B-07) |
| `0006` | `disposition`; `DISPOSITION` ledger entry type | REQ-08a/b, REQ-09 (B-08, B-09) |
| `0007` | `deadline_utc` on `disposition` (backfilled) | REQ-10 (B-10, D-23) |
| `0008` | `reminder_percent`, `reminder_deadline_utc` on `notification` | REQ-11 (B-11, D-24) |

## Diagram

```mermaid
erDiagram
    monitored_system ||--o{ metric_sample : "sends"
    monitored_system ||--o{ threshold_version : "configured with"
    monitored_system ||--o{ event : "has"
    threshold_version ||--o{ event : "breached by"
    metric_sample ||--o| event : "triggers (breach only)"
    event ||--o{ harm_field : "Art. 3(49) fields (breach only)"
    event ||--o{ disposition : "tiers, oldest first (breach only)"
    disposition ||--o| report : "Article 73 report of a serious tier"

    monitored_system {
        uuid id PK
        text name UK
        int sampling_interval_s "default 300, > 0"
        int gap_missed_intervals "default 3, > 0"
        text_array harm_fields "Art. 3(49) fields, default empty"
        timestamptz last_seen_at "nullable"
        timestamptz created_at
    }
    metric_sample {
        uuid system_id PK,FK
        text metric_name PK
        timestamptz sampled_at PK
        text metric_type "performance | drift"
        numeric value "not NaN"
        text snapshot_ref "nullable"
        timestamptz received_at
    }
    threshold_version {
        uuid id PK
        uuid system_id FK
        text metric_name
        text metric_type "performance | drift"
        text direction "above | below"
        numeric limit_value "not NaN"
        timestamptz valid_from
        uuid changed_by "nullable, FK added by auth"
    }
    event {
        uuid id PK
        text type "threshold_breach | monitoring_gap"
        text state "breach: FLAGGED..CLOSED; gap: GAP_OPEN | REVIEWER_NOTIFIED | GAP_CLOSED"
        uuid system_id FK
        text metric_name "breach only"
        timestamptz metric_timestamp "breach only"
        numeric observed_value "breach only"
        uuid threshold_version_id FK "breach only"
        numeric threshold_value "breach only"
        timestamptz detected_at
        timestamptz retain_until ">= detected_at + 6 months"
        text evidence_bundle_hash "nullable, D-05"
        timestamptz gap_started_at "gap only"
        timestamptz gap_ended_at "gap only, set when closed"
    }
    harm_field {
        uuid event_id PK,FK
        text name PK "one of the four Art. 3(49) fields"
        text status "PENDING | CONFIRMED | NOT_APPLICABLE"
        text prefill_evidence "nullable, D-20"
        text evidence "nullable, the reviewer's"
        uuid confirmed_by FK "nullable"
        timestamptz confirmed_at "nullable"
    }
    disposition {
        uuid event_id PK,FK
        int seq PK "1, 2, ..."
        text tier "DEATH | CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD | OTHER_SERIOUS | NOT_SERIOUS"
        text previous_tier "null only for seq 1"
        uuid reviewer_id FK
        text rationale "required for NOT_SERIOUS"
        timestamptz recorded_at
        timestamptz deadline_utc "null only for NOT_SERIOUS"
    }
    report {
        uuid event_id PK,FK
        int disposition_seq PK,FK "the disposition reported on"
        text document "JSON text served by GET .../report, never rewritten"
        text report_sha256 "sha256 of document"
        timestamptz assembled_at
        uuid assembled_by FK
        timestamptz submitted_at "nullable, set once"
        uuid submitted_by FK "nullable, with submitted_at"
        text submission_note "nullable"
    }
```

## Conventions (apply to every table, including teammates')

- **Timestamps** are `timestamptz`, stored and compared in UTC. Code reads "now" only through
  `app.core.clock`, never `datetime.now()` or SQL `now()`, so the test clock works.
  (`monitored_system.created_at` uses `now()` as a server default because nothing time-sensitive
  depends on it.)
- **Metric values and limits** are `numeric` (Python `Decimal`), never float, so comparisons at the
  threshold are exact (AC-03). `'NaN'` is rejected by CHECK constraints.
- **Enumerations** are `text` with a `CHECK ... IN (...)` constraint (helper: `app.db.base.check_in`),
  not native PostgreSQL ENUMs, so the value sets can change with a one-line migration.
- **Foreign keys** use `ON DELETE RESTRICT`. Nothing cascades: records needed for retention and audit
  can't disappear as a side effect of another delete.
- **Constraint names** follow `app.db.base.NAMING_CONVENTION` (`pk_`, `fk_`, `uq_`, `ck_`, `ix_`).
- **IDs** are UUIDs generated by the application (`uuid4`).

## Tables

### `monitored_system` (module `systems`, shared)
One row per monitored high-risk AI system.
- `sampling_interval_s`: REQ-01 configurable sampling interval (default 300 s = 5 min).
- `last_seen_at`: updated by metric ingestion (REQ-01). The monitoring-gap sweeper (REQ-02) reads it.
- `gap_missed_intervals`: REQ-02 gap timeout, in sampling intervals (migration 0003).
- `harm_fields`: the Art. 3(49) fields this system's use can affect (REQ-07, D-20, migration 0004).
  A breach prefills these fields' evidence; the others start blank. `ck_monitored_system_harm_fields_known`
  allows only the four field names. Empty by default.

### `metric_sample` (module `metrics`, REQ-01)
Raw performance and drift metrics, one row per measurement.
- **Primary key** `(system_id, metric_name, sampled_at)` makes ingestion idempotent (Model 2). What
  the API returns for a repeated submission is still open: D-04.
- `sampled_at` is the API's `timestamp`, the time of the measurement. `received_at` is the server
  time it arrived (an addition to Model 2, used for AC-01 and late-data detection).
- `metric_type` and the name `metric_name` match the API (D-02).
- Model 2 says samples are retained at least six months (REQ-05). See *Retention* below.

### `threshold_version` (module `thresholds`, REQ-03)
Versioned threshold configuration per `(system_id, metric_name)`.
- **Insert-only.** Changing a threshold means inserting a new row with a later `valid_from`; rows are
  never updated. `UNIQUE (system_id, metric_name, valid_from)`.
- **Threshold in force** for a sample: the row with the greatest `valid_from <= sampled_at`.
- `direction` (D-01): `above` breaches when `value > limit_value` (e.g. drift); `below` breaches
  when `value < limit_value` (e.g. accuracy, Model 5 Ex-1). Comparison is **strict**, so a value equal
  to the limit never breaches (AC-03). Implemented in `thresholds.models.Direction.breaches`.
- `changed_by` has no FK yet. Model 2 links it to the users table, which the auth module owns;
  the auth owner adds the constraint.

### `event` (module `events`, REQ-03 creation, REQ-05 storage), shared
Flagged threshold breaches and monitoring gaps. Type and state names follow the API (D-03).
- **Breach events** (written by `events`, Phase 3): always `state = 'FLAGGED'` on creation. The
  `ck_event_breach_fields_present` constraint guarantees every REQ-03 field is recorded: metric name,
  metric timestamp, observed value, threshold version and threshold value (a copy of the limit at
  flag time).
- **Triggering sample**: `fk_event_triggering_sample` links `(system_id, metric_name, metric_timestamp)`
  to `metric_sample`. The sample must exist and can't be deleted while the event exists.
- **One event per sample**: the unique constraint `uq_event_triggering_sample` prevents a duplicate
  submission from creating a second breach event (D-04). Gap events leave these columns NULL, and
  NULLs never conflict. The constraint's index also serves the RESTRICT check of
  `fk_event_triggering_sample` when a sample is deleted (the Phase 5 retention purge).
- `detected_at`: clock time when the event was flagged (REQ-15). Set once and never updated (INV-13).
- `retain_until`: see *Retention*.
- `evidence_bundle_hash`: sha256 hex of the evidence bundle. **Nullable and not computed yet**
  (D-05).
- Indexes support `GET /v1/events`: `(detected_at, id)` for the default newest-first ordering, plus
  `(system_id, detected_at)`, `state` and `type` for the filters.

### `harm_field` (module `review`, REQ-07)
The four Art. 3(49) fields of each breach event (B-07). Primary key `(event_id, name)`.
- Created in the flagging transaction, all four `PENDING`. `prefill_evidence` holds the breach's own
  facts for the fields the system lists in `monitored_system.harm_fields` (D-20), and is null for the
  rest. It is never changed afterwards.
- `status` moves to `CONFIRMED` or `NOT_APPLICABLE` when a reviewer confirms the field in a
  disposition call (REQ-08a), with `confirmed_by`, `confirmed_at` and the reviewer's own `evidence`.
  A later call may confirm a field again; the row then holds the latest confirmation and the
  ledger keeps every one (D-22).
  `ck_harm_field_confirmed_at_matches_status` and `ck_harm_field_pending_has_no_reviewer` keep the
  confirmation columns consistent with `status`.
- Monitoring-gap events have no rows (INV-12). Breach events flagged before migration 0005 have none
  either; the API shows their `harm_fields` as null until their first disposition call, which
  opens the four rows (without prefill) before applying the confirmations.
- `event_id` and `confirmed_by` are `RESTRICT` foreign keys, so an event with harm fields can't be
  deleted.

### `disposition` (module `review`, REQ-08a/b, REQ-09)
Every tier a reviewer assigns to a breach event (B-08, B-09). Primary key `(event_id, seq)`.
- Insert-only: each `POST /v1/events/{id}/disposition` adds a row with the next `seq` (from 1), so
  a revision never overwrites the earlier tier. The latest row is the event's current tier, and
  `GET /v1/events/{id}` lists every row as `revision_history`.
- `previous_tier` is the tier the row replaced: null for the first row and set for every later one
  (`ck_disposition_previous_tier_matches_seq`).
- `tier` is one of the four Article 73 tiers; `NOT_SERIOUS` needs a non-blank `rationale`
  (`ck_disposition_not_serious_has_rationale`).
- `reviewer_id` references `app_user`; `recorded_at` is the clock time of the call. The event row
  is locked while a disposition is recorded, so concurrent calls get consecutive numbers.
- The row, the harm-field confirmations, the event's new state (`DISPOSITIONED`, or `CLOSED` for
  `NOT_SERIOUS`) and one `DISPOSITION` ledger entry commit in one transaction.
- `deadline_utc` is the Article 73 deadline in force after that disposition (B-10, D-23); the
  latest row's value is the event's `deadline_utc`. `A`, the anchor of every deadline, is the
  `recorded_at` of the event's first serious-tier row. `ck_disposition_deadline_matches_tier`
  requires a deadline for every serious tier and none for `NOT_SERIOUS`. Migration 0007
  backfilled earlier rows by replaying each event's dispositions.

### `report` (module `review`, REQ-12)
The Article 73 report of one serious-tier disposition (B-12, D-25). Primary key
`(event_id, disposition_seq)`, a RESTRICT foreign key to `disposition`, so a reported
determination can't be deleted.
- Inserted by the first `GET /v1/events/{id}/report` after that disposition; `document` is the
  exact JSON text every later call returns, and `report_sha256` its sha256
  (`ck_report_report_sha256_hex`). Nothing rewrites it: a revision gets a new row on its next
  request.
- `submitted_at` and `submitted_by` are set together, once, by
  `POST /v1/events/{id}/report/submission` (`ck_report_submitted_by_matches_at`); a
  `submission_note` needs a submission (`ck_report_note_needs_submission`).
- Each assembly and submission writes one `REPORT_ASSEMBLED` / `REPORT_SUBMITTED` ledger entry in
  the same transaction (migration 0009 adds both types).

### `app_user`, `api_key` (module `auth`, REQ-13)
- `app_user.id` is the `actor_id` written to the ledger. `role` is `system | viewer | reviewer |
  admin`; `ck_app_user_system_scope` makes system accounts name exactly one `system_id` and human
  accounts none. `threshold_version.changed_by` references it.
- `api_key` stores only `key_hash` (sha256 of a 256-bit random key, unique) and a short
  `key_prefix` to tell keys apart. Revoked keys keep their row (`revoked_at`).

### `ledger_entry` (module `ledger`, REQ-06, REQ-14)
- Append-only hash chain: `seq` (gapless, from 1), `prev_hash` = previous `entry_hash`
  (`uq_ledger_entry_prev_hash` forbids forks), `entry_hash` = sha256 of the canonical entry
  (D-14). `event_id` references `event` with RESTRICT, so an audited event can't be deleted.
- `actor_id` deliberately has no FK, so an export can be verified on its own.
- Tampering is detected, not prevented (Ex-5, AC-06): `GET /v1/ledger/verify` re-walks the chain.
- `entry_type` (`ck_ledger_entry_entry_type`): `FLAGGED`, `VIEW`, `GAP_OPENED`, `GAP_CLOSED`,
  `NOTIFIED`, `DISPOSITION` (0006), `REPORT_ASSEMBLED`, `REPORT_SUBMITTED` (0009) and `REFUSED`
  (0010: a refused disposition or report call on an existing event, D-26).

### `webhook`, `notification`, `notification_delivery` (module `notifications`, REQ-04)
- `notification` is the outbox row written in the event's transaction; `deliver_by` = created +
  `NOTIFY_MAX_DELAY_S`. `dispatched_at` stays NULL until a subscribed webhook exists.
- `notification_delivery` is one row per (notification, webhook), with status, attempts, the
  next attempt time and the last error. Partial indexes serve the worker's two queries.
- **Reminders (REQ-11, migration 0008).** A `reminder` row records its checkpoint
  (`reminder_percent`: 50, 80 or 100) and the deadline in force when it was queued
  (`reminder_deadline_utc`); other types leave both NULL (`ck_notification_reminder_fields_match_type`).
  `uq_notification_reminder_once` (unique on `event_id, reminder_percent` where set) sends each
  checkpoint once per event, even after a revision moves the deadline.
- **Ledger alerts (Ex-5, migration 0011).** A `ledger_alert` row is about the ledger, not an
  event: it's the only type with `event_id` NULL (`ck_notification_event_id_matches_type`).
  Webhooks may subscribe to it, and its delivery body has `alert` instead of `event_id`/`event`.

### `ledger_check` (module `ledger`, REQ-06, migration 0011)
One row per scheduled verification by the worker (#71, D-27). Insert-only.
- `valid`, `entries_checked`, `first_mismatch`, and the verified head (`head_seq`, `head_hash`).
  `problem` says what failed, and is set exactly when `valid` is false
  (`ck_ledger_check_valid_iff_no_problem`).
- The latest passing check's head is what the next one compares against, to catch entries
  removed from the end of the chain.
- `alerted` marks the first failure of an incident, which queued a `ledger_alert`
  (`ck_ledger_check_only_failures_alert`).
- It lives in the same database as the ledger, so someone with full database access could rewrite
  both. Shipping the head to an outside store is still open (D-27).

### Monitoring gaps (module `monitoring_gaps`, REQ-02) on `event`
- `monitored_system.gap_missed_intervals` (default 3) sets the timeout per system.
- Gap events have `type = 'monitoring_gap'`, their own states (D-12), `gap_started_at` (last data
  before the gap) and `gap_ended_at` (first data after it, set when closed). CHECKs pair states
  with types (INV-12) and require the gap fields. `uq_event_one_open_gap_per_system` allows one
  open gap per system. `ix_metric_sample_system_id_received_at` finds when data resumed.

### `simulated_clock`, `worker_heartbeat`
Single-row tables: the test environment's clock offset, shared by the API and the worker (D-16),
and the worker's liveness for `/healthz`.

## Retention (REQ-05): storage only

Phase 3 implements the **storage** part of REQ-05:
- `event.retain_until` is stored for every event. `app.modules.events.models.retain_until_for()`
  computes it as `detected_at` + 6 calendar months in UTC.
- `ck_event_retain_until_min_6_months` makes the database reject any event whose `retain_until` is
  less than six months after `detected_at` (INV-10). The month arithmetic is pinned to UTC so the
  check doesn't depend on the connection's time zone.
- `RESTRICT` foreign keys stop a monitored system, threshold version or triggering sample from being
  deleted while an event references it.

**Not implemented yet** (Phase 5, retention half of REQ-05; AC-05):
- Nothing currently *prevents* deleting an `event` row or a non-triggering `metric_sample` before
  `retain_until`, other than the foreign keys above.
- No job removes data after its retention period, and nothing protects the ±1 h evidence-window
  samples of an event (D-05).
- Backup retention (Lightsail keeps 7 days of point-in-time restore) is not part of this.

## Extending the schema

1. Create `app/modules/<module>/models.py` using `app.db.base.Base`, and follow the conventions above.
2. Import the module in `app/db/models.py` so Alembic sees it.
3. Add a migration (`alembic revision --autogenerate -m "..."` once a database is available) and
   review it by hand. Never edit a migration that has already been merged.
4. **Reference events with** `event_id uuid NOT NULL REFERENCES event(id) ON DELETE RESTRICT`.

Still to come (Model 2):

| Table / change | Phase | Links to |
|---|---|---|
| `record_key` (REQ-16 crypto-shredding) | 5 | `event.id` |
| `art349_field`, `disposition`, `escalation`, `report` | 4–5 | `event.id` |
| new `ledger_entry.entry_type` values (`FIELD_CONFIRMED`, `DISPOSITION`, ...) | 4 | alter `ck_ledger_entry_entry_type` |

Changing `event.state` or `event.type` values: alter the `ck_event_state` / `ck_event_type` CHECK
constraints in a migration, update `EventState` / `EventType`, and update D-03 in spec-decisions.md.
