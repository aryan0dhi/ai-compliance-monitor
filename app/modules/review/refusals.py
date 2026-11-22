"""Audit entries for refused review calls (B-14, Model 5 Ex-4; D-26).

A refused disposition or report call changes nothing, and its own transaction rolls back. So that
the audit trail still shows the attempt, the route records one REFUSED ledger entry afterwards,
in a separate transaction, when the refusal is a 403 or 409 on an event that exists. Unknown or
malformed ids and invalid bodies (400) are not recorded: they name no event, and logging them
would let anyone grow the ledger with made-up ids.

Recording is best effort. If it fails, the caller still gets the refusal it earned (nothing
happened to the event), and the failure is logged.
"""

import logging
from uuid import UUID

from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter

logger = logging.getLogger(__name__)


def record_refusal(
    event_id: UUID | None,
    *,
    operation: str,
    status_code: int,
    reason: str,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
) -> bool:
    """Append one REFUSED entry for a refused call on an existing event. True if written."""
    if event_id is None:
        return False
    try:
        with uow:
            if uow.events.get(event_id) is None:
                return False
            ledger.append(
                uow.session,
                entry_type=LedgerEntryType.REFUSED,
                actor_id=principal.actor_id,
                payload={
                    "event_id": str(event_id),
                    "operation": operation,
                    "status": status_code,
                    "role": str(principal.role),
                    "reason": reason,
                },
                event_id=event_id,
            )
    except Exception:
        logger.exception("could not record a refused %s on event %s", operation, event_id)
        return False
    return True
