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
