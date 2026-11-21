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

Migration line: `0009` on main → `0010` (#73) → `0011` (#74). New schema work stacks after `0011`.

## Work log

| Item | Branch / PR | What was done | Tests | CI | Merge status |
|---|---|---|---|---|---|
| REQ-16 erasure (#70), end-of-retention job | `work-issue-70-crypto-shredding`, stacked on #74 | Per-event AES-256-GCM record keys wrapped by `RECORD_KEY_MASTER_KEY`; rationale, reviewer evidence, report document and submission note stored and ledgered sealed; worker destroys the key at `max(retain_until, detected_at + 6 months + 1 day)` with a `PERSONAL_DATA_ERASED` entry; migration `0012` seals existing rows; D-28 | Local, PostgreSQL 16: 981 passed, 0 failed, 43 skipped (live contract suite only); ruff, mypy, bandit, pip-audit clean | see PR | Open; waits for #73 and #74 |

## Blockers and decisions needed

- **#73 → #74 merge order** (owner EthanBar25). Everything after them stacks on migration `0011`.
- **D-28 (pod):** the additive `personal_data_erased_at` field and `409` for an erased report.
- **D-05 (pod):** evidence-bundle window, before #68 can be built without inventing a rule.
- **D-18 (pod):** AC-05 checkpoint. Erasure waits one extra day so either reading passes.
- **Deployment (needs approval):** the VM still runs pre-Phase-4 code. Deploying `0012` adds
  `RECORD_KEY_MASTER_KEY` to the VM's `.env` (created by `deploy/gcp/remote-up.sh`); back it up.
