"""What the contract suite can cover (Phase 3 scope, Section 6.1).

Shared by the suite's own report (conftest.py) and the tester service (tester/), so both count
coverage against the same lists. Imports nothing from the application.
"""

PHASE3_REQS = ["REQ-01", "REQ-02", "REQ-03", "REQ-04", "REQ-05", "REQ-06", "REQ-13", "REQ-14"]
PHASE3_ACS = ["AC-01", "AC-02", "AC-03", "AC-04", "AC-06", "AC-15"]
ENDPOINTS = [
    "POST /v1/metrics",
    "GET /v1/events",
    "GET /v1/events/{id}",
    "GET /v1/events/{id}/audit",
    "GET /v1/ledger/export",
    "GET /v1/ledger/verify",
    "POST /v1/webhooks",
    "POST /v1/test/clock/advance",
    "POST /v1/test/reset",
]
# Model 1 elements reachable in Phase 3 (monitoring states, gap path, denial).
MODEL1 = ["FLAGGED", "DENIED", "GAP_OPEN", "REVIEWER_NOTIFIED", "GAP_CLOSED"]
# Model 4 invariants checkable through the API in Phase 3.
MODEL4 = ["INV-7", "INV-8", "INV-10", "INV-12", "INV-13"]
# Model 5 worked examples (docs/pod-interface/fixtures.json scenarios).
MODEL5 = ["Ex-1", "Ex-2", "Ex-3", "Ex-4", "Ex-5"]

# --- Phase 5 milestone (course testing interface, "Coverage fields"; D-31) ------------------------
# What the tester's suite report counts against. Each test declares what it can exercise with
# @covers; a Model 5 scenario reports only what its executed steps evaluated (scenarios.py).

# system: the acceptance criteria of the pod specification (Phase 2 §6.2). All are in scope for
# Phase 5 (§6.4: Phase 5 adds AC-13, AC-16 and AC-18; earlier criteria carry over).
ACCEPTANCE_CRITERIA = [f"AC-{n:02d}" for n in range(1, 19)]
# component: the operations of the pod interface (docs/pod-interface/openapi.yaml) up to Phase 5.
COMPONENTS = [
    "POST /v1/metrics",
    "GET /v1/events",
    "GET /v1/events/{id}",
    "GET /v1/events/{id}/audit",
    "GET /v1/ledger/export",
    "GET /v1/ledger/verify",
    "POST /v1/webhooks",
    "GET /v1/me",
    "GET /v1/meta",
    "GET /v1/test/clock",
    "POST /v1/test/clock/advance",
    "POST /v1/test/reset",
    "POST /v1/events/{id}/disposition",
    "GET /v1/events/{id}/report",
    "GET /v1/events/{id}/reports",
    "GET /v1/reports/{id}",
    "POST /v1/events/{id}/report/submission",
]
# model (own system only): the design requirements our team lists for Phase 5, from the Phase 4
# design. docs/testing-interface/model-coverage.md maps each one to the tests that evaluate it.
MODEL1_STATES = [
    "FLAGGED",
    "UNDER_REVIEW",
    "DISPOSITIONED",
    "CLOSED",
    "GAP_OPEN",
    "REVIEWER_NOTIFIED",
    "GAP_CLOSED",
    "DENIED",
]
DESIGN_REQUIREMENTS = MODEL1_STATES + MODEL4 + MODEL5
DESIGN_VERSION = "Phase 4 design models (Models 1, 4 and 5)"
MODEL_MAPPING = "docs/testing-interface/model-coverage.md"
