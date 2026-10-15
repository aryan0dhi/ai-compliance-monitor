"""Ledger interface.

Every read or write of an event record must append exactly one ledger entry in the SAME database
transaction as the change it records (Phase 2, Q7.2 Decision 1; INV-8).

NOT IMPLEMENTED. `append` raises UnimplementedError, which aborts the caller's transaction, so no
state change can commit without its audit entry.
"""

from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import UnimplementedError


def append(
    session: Session,
    *,
    entry_type: str,
    actor_id: UUID | None,
    payload: dict[str, Any],
    event_id: UUID | None = None,
) -> None:
    """Append a hash-chained ledger entry using the caller's session/transaction."""
    raise UnimplementedError("Audit ledger append", "REQ-06/REQ-14", "ledger")
