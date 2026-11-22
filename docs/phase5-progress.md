# Phase 5 implementation progress

A running record of the Phase 5 coding work (Phase 2 §6.3), so anyone picking it up can see what
was found, what was done, and what is still open. Newest entries at the bottom of each table.
No secrets go in this file.

## Starting point (2026-11-21, `origin/main` = `6e83837`)

| Requirement | State on `main` | Open PR / branch | Owner |
|---|---|---|---|
| REQ-07, 08a/b, 09, 10, 11, 13 (Phase 4) | Merged (#51–#58), unit + PostgreSQL tested | — | EthanBar25 |
| REQ-12 report + submission (JSON) | Merged (#64) | — | EthanBar25 |
| REQ-14 / Model 5 Ex-4 refused attempts; phase 4 in `/v1/meta` | Not on main | #73 (CI green, no review) | EthanBar25 |
| REQ-06 scheduled ledger check + critical alert | Not on main | #74, stacked on #73 (CI green) | EthanBar25 |
| `/v1/meta` build SHA; README/HANDOFF refresh | Not on main | #65, #66 (stacked) | EthanBar25 |
| REQ-16 personal-data erasure | Nothing | none (#70 unassigned) | — |
| REQ-05 retention enforcement | `retain_until` + CHECK only | none (#69 unassigned) | — |
| REQ-05 evidence-bundle hash | Column only, never set | none (#68 unassigned; D-05 open) | — |
| Phase 5 listed in `/v1/meta` | — | #72 (tracking) | — |
| Deployment | GCP VM runs pre-Phase-4 code (`/v1/meta` phases `[3]`, no review routes) | — | — |

Migration line: `0009` on main → `0010` (#73) → `0011` (#74) → `0012` (#75) → `0013` (#76). All on
`main` since 2026-11-22; new schema work starts from `0013`.

## Work log

| Item | Branch / PR | What was done | Tests | CI | Merge status |
|---|---|---|---|---|---|
| REQ-16 erasure (#70), end-of-retention job | `work-issue-70-crypto-shredding`, stacked on #74, PR #75 | Per-event AES-256-GCM record keys wrapped by `RECORD_KEY_MASTER_KEY`; rationale, reviewer evidence, report document and submission note stored and ledgered sealed; worker destroys the key at `max(retain_until, detected_at + 6 months + 1 day)` with a `PERSONAL_DATA_ERASED` entry; migration `0012` seals existing rows; D-28 | Local, PostgreSQL 16: 981 passed, 0 failed, 43 skipped (live contract suite only); ruff, mypy, bandit, pip-audit clean | After updating with `main` (`58ae704`): all 17 checks green; 988 passed, 43 skipped (contract job 43/43), coverage 96.43 % | **Merged 2026-11-22** as `5ce4c20` (after #73 `315346a` and #74 `632d3a3`); closed #70 |
| REQ-05 retention guard (#69, part) | `work-issue-69-retention-guard`, stacked on #75 | Migration `0013`: trigger refuses deleting an event, or lowering `retain_until`, before retention on the service clock (incl. simulated offset); row deletion after retention not added (D-28 erases personal data instead) | Local, PostgreSQL 16: 988 passed, 0 failed, 43 skipped; ruff, mypy clean | At `cf35a5e`: all 17 checks green; 995 passed, 43 skipped (contract job 43/43), coverage 96.43 % | **Merged 2026-11-22** as `37d0978`; #69 stays open (purge question, D-28) |
| Operations note: signals, alerts, backups, recovery, release checklist (Phase 2 §6.3) | `docs-operations-note`, stacked on #76 | `docs/operations.md`; backup schedule and restore steps are recommendations, not run | docs only | PR #77 | This PR |

## Not started, and why

- **#68 evidence-bundle hash:** D-05 (which window, when hashed) is an open pod decision; building
  it now would mean inventing the rule.
- **#72 phase 5 in `/v1/meta`:** last, after #73, #74, #75, #76 and a D-05 answer; listing phase 5
  makes podmates' harnesses run Phase 5 steps against us.
- **Chain head outside the database (REQ-06, §6.3):** follow-up to #74 (owner EthanBar25), noted as
  a limit in D-27.
- **REQ-15 explicit elapsed-time field, REQ-10 configurable calendar:** both change the shared
  contract (B-15, B-10); pod decision first.
- **PDF reports, health dashboard:** Phase 2 §6.4 allows dropping both; needs pod agreement.

## Blockers and decisions needed

- **D-28 (pod):** the additive `personal_data_erased_at` field and `409` for an erased report.
- **D-05 (pod):** evidence-bundle window, before #68 can be built without inventing a rule.
- **D-18 (pod):** AC-05 checkpoint. Erasure waits one extra day so either reading passes.
- **Deployment (needs approval):** the VM still runs pre-Phase-4 code. Deploying `0012` adds
  `RECORD_KEY_MASTER_KEY` to the VM's `.env` (created by `deploy/gcp/remote-up.sh`); back it up.
