"""Ledger export, verification (REQ-06, AC-06) and per-event audit trail (REQ-14, AC-15).

Reading the ledger or an audit trail is not itself logged: these endpoints show the audit
records, not the event records INV-8 covers, and logging them would grow the ledger on every
verification pass (docs/spec-decisions.md D-14).
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.principal import READER_ROLES, Principal, Role
from app.modules.ledger.chain import VerifyResult, verify_chain
from app.modules.ledger.checks import run_ledger_check
from app.modules.ledger.models import LedgerCheck, LedgerEntry, VerificationTrigger
from app.modules.notifications.outbox import notify_max_delay

logger = logging.getLogger(__name__)

VERIFY_BATCH_SIZE = 1000


class LedgerAccessDenied(Exception):
    """The caller's role may not read the ledger."""


class AuditEventNotFound(Exception):
    pass


@dataclass(frozen=True)
class LedgerPage:
    entries: list[LedgerEntry]
    # Pass as after_seq to get the next page; None when this page reached the end.
    next_after_seq: int | None


def _require_reader(principal: Principal) -> None:
    if principal.role not in READER_ROLES:
        raise LedgerAccessDenied(f"role {principal.role} may not read the ledger")


def export_ledger(
    *, after_seq: int, limit: int, principal: Principal, uow: UnitOfWork
) -> LedgerPage:
    """Raw entries and hashes, for re-verification outside this service (Q4.3)."""
    _require_reader(principal)
    with uow:
        entries = uow.ledger.page(after_seq=after_seq, limit=limit)
    next_after = entries[-1].seq if len(entries) == limit else None
    return LedgerPage(entries=entries, next_after_seq=next_after)


def verify_ledger(*, principal: Principal, uow: UnitOfWork) -> VerifyResult:
    """Re-walk the whole chain (Section 5.1 GET /v1/ledger/verify; Ex-5)."""
    _require_reader(principal)
    with uow:

        def entries() -> Iterator[LedgerEntry]:
            after = 0
            while page := uow.ledger.page(after_seq=after, limit=VERIFY_BATCH_SIZE):
                yield from page
                after = page[-1].seq

        result = verify_chain(entries())
    if not result.valid:
        logger.critical(
            "ledger verification failed: chain broken at seq %s (%s entries checked)",
            result.first_mismatch,
            result.entries_checked,
        )
    return result


def event_audit(event_id: UUID, *, principal: Principal, uow: UnitOfWork) -> list[LedgerEntry]:
    """Every ledger entry recorded for one event: who viewed or changed it, and when."""
    _require_reader(principal)
    with uow:
        if uow.events.get(event_id) is None:
            raise AuditEventNotFound(str(event_id))
        return uow.ledger.for_event(event_id)


# --- verification runs (contract delta Δ5; D-29) -----------------------------------------

# Who may start a run on request; reading runs is open to every reader role.
VERIFICATION_RUNNERS = frozenset({Role.REVIEWER})


class VerificationRunDenied(Exception):
    """Only reviewers start a verification run (403)."""


def list_verifications(*, principal: Principal, uow: UnitOfWork) -> list[LedgerCheck]:
    """GET /v1/ledger/verifications: every run, scheduled or manual, newest first."""
    _require_reader(principal)
    with uow:
        return uow.ledger_checks.newest_first()


def run_verification(*, principal: Principal, uow: UnitOfWork, clock: Clock) -> LedgerCheck:
    """POST /v1/ledger/verifications: verify the whole chain now and record it as a MANUAL run.
    A failure alerts exactly as a scheduled run does (D-27)."""
    if principal.role not in VERIFICATION_RUNNERS:
        raise VerificationRunDenied(f"role {principal.role} may not start a verification")
    result = run_ledger_check(
        uow,
        clock=clock,
        interval=timedelta(0),
        max_delay=notify_max_delay(),
        force=True,
        trigger=VerificationTrigger.MANUAL,
    )
    if result is None:  # pragma: no cover - force always runs
        raise RuntimeError("a forced verification always runs")
    return result.check
