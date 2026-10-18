"""Ledger interface.

Every read or write of an event record must append exactly one ledger entry in the SAME database
transaction as the change it records (Phase 2, Q7.2 Decision 1; INV-8).

Callers receive a `LedgerWriter` through `get_ledger()` (a FastAPI dependency) and pass their
unit-of-work session to `append`. The production writer (`SqlLedgerWriter`) hash-chains each entry
to the previous one (REQ-06); if it fails, the caller's transaction rolls back, so no state change
or view commits without its audit entry.
"""

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.modules.ledger.models import LedgerEntryType
from app.modules.ledger.writer import SqlLedgerWriter

__all__ = ["LedgerEntryType", "LedgerWriter", "get_ledger"]


class LedgerWriter(Protocol):
    def append(
        self,
        session: Session | None,
        *,
        entry_type: str,
        actor_id: UUID | None,
        payload: dict[str, Any],
        event_id: UUID | None = None,
    ) -> None:
        """Append a hash-chained ledger entry using the caller's session/transaction."""
        ...


_ledger: LedgerWriter = SqlLedgerWriter()


def get_ledger() -> LedgerWriter:
    """FastAPI dependency returning the production ledger writer."""
    return _ledger
