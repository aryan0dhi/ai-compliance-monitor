# Model coverage: design requirements to tests (Phase 5)

The course testing interface asks the tester's suite report to carry `coverage.model` from Phase 5
on, **only when the tester tests its own team's system** (cross-team reports omit it):

- `total` is the number of design requirements our team lists for the milestone;
- `covered` is how many of them the suite evaluated;
- `basis` names the design version and links here.

Our tester includes it when the target is one of `TESTER_SELF_URLS` (D-31).

**Design version:** the Phase 4 design models (Models 1, 4 and 5, as updated in the Phase 4
submission, §5). **The list** (`DESIGN_REQUIREMENTS` in
[`tests/contract/scope.py`](../../tests/contract/scope.py)) has 18 entries: the eight Model 1
states, the five Model 4 invariants the design marks as checkable through the API, and the five
Model 5 worked examples.

**What counts.** A requirement is evaluated when a test that declares it finished with `pass` or
`fail`; `error` and `skip` evaluate nothing. A Model 5 scenario declares only what its executed
steps evaluated (`evaluated_tags` in [`tests/contract/scenarios.py`](../../tests/contract/scenarios.py)):
the states its steps checked, and the example itself only when no step was deferred by the
target's `GET /v1/meta` phases.

| Requirement | Design element | Evaluated by (tests/contract) |
| --- | --- | --- |
| FLAGGED | Model 1 state | `test_ac03_drift_breach_records_every_required_field`; scenarios Ex-1, Ex-2 (step `flagged`), Ex-4 |
| UNDER_REVIEW | Model 1 state | scenario Ex-1 step `under-review`, Ex-4 step `premature-report` (phase 4) |
| DISPOSITIONED | Model 1 state | scenario Ex-1 step `assign-other-serious` (phase 4) |
| CLOSED | Model 1 state | scenario Ex-2 step `not-serious` (phase 4) |
| GAP_OPEN | Model 1 state | `test_ac02_gap_after_three_missed_intervals_not_two` |
| REVIEWER_NOTIFIED | Model 1 state | `test_ac02_gap_notification_reaches_the_reviewer` |
| GAP_CLOSED | Model 1 state | `test_ac02_gap_after_three_missed_intervals_not_two` |
| DENIED | Model 1 element (refused action) | `test_roles_are_enforced`, `test_only_admins_register_webhooks`; scenario Ex-4 step `viewer-denied` |
| INV-7 | Model 4 invariant (hash chain) | `test_ac06_ledger_verifies_and_an_edited_export_is_caught_offline`, `test_ledger_still_verifies_after_the_whole_run` |
| INV-8 | Model 4 invariant | `test_ac15_every_view_is_logged_with_identity_and_time` |
| INV-10 | Model 4 invariant (retention ≥ 6 months) | `test_ac03_drift_breach_records_every_required_field` |
| INV-12 | Model 4 invariant (gaps never reviewed) | `test_ac02_gap_after_three_missed_intervals_not_two` |
| INV-13 | Model 4 invariant (`detected_at` set once) | `test_ac15_every_view_is_logged_with_identity_and_time` |
| Ex-1 | Model 5 example (serious, revised upward) | `test_model5_ex1_serious_incident_flag`, when every step ran (its report step is phase 5) |
| Ex-2 | Model 5 example (not serious) | `test_model5_ex2_not_serious_flag`, when every step ran |
| Ex-3 | Model 5 example (monitoring gap) | `test_ac02_gap_after_three_missed_intervals_not_two`, `test_ac02_gap_notification_reaches_the_reviewer` |
| Ex-4 | Model 5 example (viewer can't act) | `test_model5_ex4_viewer_cannot_act_but_is_logged`, when every step ran |
| Ex-5 | Model 5 example (tampering detected) | `test_ac06_ledger_verifies_and_an_edited_export_is_caught_offline` |

`tests/test_tester.py` checks that this table lists exactly `DESIGN_REQUIREMENTS`, and that every
requirement here is declared by at least one contract test or scenario step.
