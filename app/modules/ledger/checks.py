"""Scheduled ledger verification with a critical alert (REQ-06, Model 5 Ex-5, #71; D-27).

`run_ledger_check` is called on every worker pass and does real work once per interval of the
service clock (daily by default, so the test clock can bring it forward):

1. re-walk the whole chain, as `GET /v1/ledger/verify` does;
2. compare it with the head of the last check that passed: that entry must still exist with the
   same hash, which catches entries removed from the end of the chain (D-14);
3. store the result as a `ledger_check` row;
4. on the first failure after a passing check, queue one `ledger_alert` notification
   (`severity: critical`) for webhooks subscribed to it, and log at CRITICAL. A failure that
   persists doesn't alert again; the next passing check closes the incident.

Nothing is appended to the ledger itself: the chain may be the thing that's broken, and a daily
entry would only grow it. The check rows live in their own table.
"""

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import uuid4

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.ledger.chain import VerifyResult, verify_chain
from app.modules.ledger.models import LedgerCheck, LedgerEntry
from app.modules.notifications.models import NotificationType
from app.modules.notifications.outbox import build_notification

logger = logging.getLogger(__name__)

PAGE_SIZE = 1000
SEVERITY = "critical"


@dataclass(frozen=True)
class LedgerCheckResult:
    check: LedgerCheck
    alert_queued: bool


def _entries(uow: UnitOfWork) -> Iterator[LedgerEntry]:
    after = 0
    while page := uow.ledger.page(after_seq=after, limit=PAGE_SIZE):
        yield from page
        after = page[-1].seq


def _entry_hash_at(uow: UnitOfWork, seq: int) -> str | None:
    if seq == 0:
        return None
    page = uow.ledger.page(after_seq=seq - 1, limit=1)
    return page[0].entry_hash if page and page[0].seq == seq else None


def _problem(uow: UnitOfWork, anchor: LedgerCheck | None, result: VerifyResult) -> str | None:
    if not result.valid:
        return (
            f"the chain is broken at entry {result.first_mismatch} "
            f"({result.entries_checked} entries checked)"
        )
    if anchor is None or anchor.head_seq == 0:
        return None
    if result.head_seq < anchor.head_seq:
        return (
            f"entries removed from the end: the last passing check ended at entry "
            f"{anchor.head_seq}, the chain now ends at {result.head_seq}"
        )
    if _entry_hash_at(uow, anchor.head_seq) != anchor.head_hash:
        return (
            f"entry {anchor.head_seq} no longer has the hash recorded by the last passing "
            "check (the end of the chain was replaced)"
        )
    return None


def _alert_payload(check: LedgerCheck, anchor: LedgerCheck | None) -> dict[str, Any]:
    return {
        "severity": SEVERITY,
        "check_id": str(check.id),
        "checked_at": check.checked_at.isoformat(),
        "problem": check.problem,
        "first_mismatch": check.first_mismatch,
        "entries_checked": check.entries_checked,
        "verified_head": {"seq": check.head_seq, "hash": check.head_hash},
        "last_passing_head": (
            {
                "seq": anchor.head_seq,
                "hash": anchor.head_hash,
                "checked_at": anchor.checked_at.isoformat(),
            }
            if anchor is not None
            else None
        ),
    }


def run_ledger_check(
    uow: UnitOfWork,
    *,
    clock: Clock,
    interval: timedelta,
    max_delay: timedelta,
    force: bool = False,
) -> LedgerCheckResult | None:
    """Verify the ledger if the last check is at least `interval` old (or `force`). Returns
    None when no check was due."""
    with uow:
        now = clock.now()
        last = uow.ledger_checks.latest()
        if not force and last is not None and now < last.checked_at + interval:
            return None
        anchor = uow.ledger_checks.latest_valid()
        result = verify_chain(_entries(uow))
        problem = _problem(uow, anchor, result)
        # One alert per incident: only when the previous check passed (or there was none).
        alert = problem is not None and (last is None or last.valid)
        check = LedgerCheck(
            id=uuid4(),
            checked_at=now,
            valid=problem is None,
            entries_checked=result.entries_checked,
            head_seq=result.head_seq,
            head_hash=result.head_hash,
            first_mismatch=result.first_mismatch,
            problem=problem,
            alerted=alert,
        )
        uow.ledger_checks.add(check)
        if alert:
            uow.notifications.add(
                build_notification(
                    event_id=None,
                    notification_type=NotificationType.LEDGER_ALERT,
                    payload=_alert_payload(check, anchor),
                    now=now,
                    max_delay=max_delay,
                )
            )
    if problem is not None:
        logger.critical(
            "ledger check failed: %s%s",
            problem,
            "" if alert else " (incident already alerted)",
        )
    return LedgerCheckResult(check=check, alert_queued=alert)
