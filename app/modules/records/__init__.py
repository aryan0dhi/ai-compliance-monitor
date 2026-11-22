"""Per-event record keys: personal data that can be erased without breaking the ledger (REQ-16).

Phase 2 Q7.2 Decision 1 and Q7.1: free text a person writes about an event is encrypted with that
event's own key before it is stored or hashed into the ledger. When the event's retention period
ends (REQ-05), the worker destroys the key; the text becomes unreadable everywhere, while every
row and every ledger hash stays where it was, so `GET /v1/ledger/verify` still passes (AC-18).
"""
