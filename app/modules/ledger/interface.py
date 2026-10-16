"""Ledger interface.

Every read or write of an event record must append exactly one ledger entry in the SAME database
transaction as the change it records (Phase 2, Q7.2 Decision 1; INV-8).

Callers receive a `LedgerWriter` through `get_ledger()` (a FastAPI dependency) and pass their
unit-of-work session to `append`.

NOT IMPLEMENTED. The production writer is `UnimplementedLedger`, which raises UnimplementedError.
That aborts the caller's transaction, so no event state change or view can commit without its
audit entry. The ledger owner replaces `UnimplementedLedger` with the real hash-chain writer and
keeps the `LedgerWriter` signature.
"""

from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from app.core.errors import UnimplementedError


class LedgerEntryType(StrEnum):
    """Model 2 ledger_entry.entry_type values used so far. The ledger owner adds the rest."""

    FLAGGED = "FLAGGED"
    VIEW = "VIEW"


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


class UnimplementedLedger:
    def append(
        self,
        session: Session | None,
        *,
        entry_type: str,
        actor_id: UUID | None,
        payload: dict[str, Any],
        event_id: UUID | None = None,
    ) -> None:
        raise UnimplementedError("Audit ledger append", "REQ-06/REQ-14", "ledger")


_ledger: LedgerWriter = UnimplementedLedger()


def get_ledger() -> LedgerWriter:
    """FastAPI dependency returning the production ledger writer."""
    return _ledger
