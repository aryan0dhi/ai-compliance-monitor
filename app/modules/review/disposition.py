"""POST /v1/events/{event_id}/disposition: confirm the Art. 3(49) fields and assign or revise the
severity tier (REQ-07, REQ-08a/b, REQ-09, REQ-13; pod rules B-08, B-09).

One call is one change to the event: the harm-field confirmations, the new disposition row, the
state change and its single DISPOSITION ledger entry commit together or not at all (Q7.2
Decision 1, B-14). Each disposition stores the Article 73 deadline in force after it (B-10,
review.deadlines). Reminders (B-11, #40) are not sent yet.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event, EventState, EventType
from app.modules.events.service import EventNotFound, EventRecord
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.review.deadlines import DeadlineState, LooserTierNeedsRationale
from app.modules.review.deadlines import apply as apply_deadline
from app.modules.review.models import (
    Disposition,
    DispositionTier,
    HarmField,
    HarmFieldName,
    HarmFieldStatus,
)

# B-13: only reviewers dispose of events. Admins read and configure, they don't decide tiers.
DISPOSITION_ROLES = frozenset({Role.REVIEWER})


class DispositionDenied(Exception):
    """The caller's role may not record dispositions (403)."""


class DispositionConflict(Exception):
    """The event can't take this disposition in its current state (409)."""


class MissingRationale(ValueError):
    """NOT_SERIOUS without a non-blank rationale (400, B-08). The schema rejects it first; this
    guards callers that skip the HTTP layer."""


@dataclass(frozen=True)
class FieldConfirmation:
    status: HarmFieldStatus
    evidence: str | None = None

    def __post_init__(self) -> None:
        if self.status is HarmFieldStatus.PENDING:
            raise ValueError("a confirmation is CONFIRMED or NOT_APPLICABLE")


@dataclass(frozen=True)
class DispositionRequest:
    tier: DispositionTier
    rationale: str | None = None
    harm_fields: dict[HarmFieldName, FieldConfirmation] = field(default_factory=dict)


def has_text(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def _apply_confirmations(
    uow: UnitOfWork,
    event: Event,
    confirmations: dict[HarmFieldName, FieldConfirmation],
    *,
    reviewer: UUID,
    at: datetime,
) -> list[HarmField]:
    fields = uow.harm_fields.for_event(event.id)
    if not fields:
        # A breach flagged before migration 0005 has no rows. Open them now, without prefill
        # (D-20 prefills only at flag time), so the reviewer can still confirm all four.
        fields = [
            HarmField(event_id=event.id, name=name.value, status=HarmFieldStatus.PENDING.value)
            for name in HarmFieldName
        ]
        uow.harm_fields.add_all(fields)
    by_name = {f.name: f for f in fields}
    # A field confirmed again is updated; the ledger keeps every confirmation (B-14).
    for name, confirmation in confirmations.items():
        f = by_name[name.value]
        f.status = confirmation.status.value
        f.evidence = confirmation.evidence
        f.confirmed_by = reviewer
        f.confirmed_at = at
    return fields


def authorize_disposition(principal: Principal) -> None:
    if principal.role not in DISPOSITION_ROLES:
        raise DispositionDenied(f"role {principal.role} may not record dispositions")


def record_disposition(
    event_id: UUID,
    request: DispositionRequest,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> EventRecord:
    """Record one disposition (the first, or a revision) and return the updated event.

    Raises DispositionDenied (403), EventNotFound (404), MissingRationale (400) or
    DispositionConflict (409); nothing is written in any of those cases.
    """
    authorize_disposition(principal)
    if request.tier is DispositionTier.NOT_SERIOUS and not has_text(request.rationale):
        raise MissingRationale("NOT_SERIOUS needs a non-empty rationale")
    with uow:
        # Locked, so concurrent dispositions of one event get consecutive seq numbers.
        event = uow.events.get_for_update(event_id)
        if event is None:
            raise EventNotFound(str(event_id))
        if event.type != EventType.THRESHOLD_BREACH:
            raise DispositionConflict("monitoring-gap events are never reviewed (INV-12)")
        now = clock.now()
        fields = _apply_confirmations(
            uow, event, request.harm_fields, reviewer=principal.actor_id, at=now
        )
        pending = [f.name for f in fields if f.status == HarmFieldStatus.PENDING]
        if pending:
            raise DispositionConflict(
                "every harm field must be CONFIRMED or NOT_APPLICABLE first; pending: "
                + ", ".join(pending)
            )
        history = uow.dispositions.for_event(event.id)
        previous = history[-1] if history else None
        # The stored rows give A (first serious tier) and the deadline in force; the rules
        # (B-10) only decide the next one, so a later rule change never rewrites history.
        state = DeadlineState(
            tier=DispositionTier(previous.tier) if previous else None,
            anchor=next(
                (d.recorded_at for d in history if DispositionTier(d.tier).is_serious), None
            ),
            deadline=previous.deadline_utc if previous else None,
        )
        try:
            after = apply_deadline(
                state, request.tier, rationale=has_text(request.rationale), at=now
            )
        except LooserTierNeedsRationale as exc:
            raise DispositionConflict(str(exc)) from exc
        disposition = Disposition(
            event_id=event.id,
            seq=previous.seq + 1 if previous else 1,
            tier=request.tier.value,
            previous_tier=previous.tier if previous else None,
            reviewer_id=principal.actor_id,
            rationale=request.rationale,
            recorded_at=now,
            deadline_utc=after.deadline,
        )
        uow.dispositions.add(disposition)
        event.state = (
            EventState.DISPOSITIONED if request.tier.is_serious else EventState.CLOSED
        ).value
        ledger.append(
            uow.session,
            entry_type=LedgerEntryType.DISPOSITION,
            actor_id=principal.actor_id,
            payload=_ledger_payload(event, disposition, request.harm_fields),
            event_id=event.id,
        )
        dispositions = [*history, disposition]
    return EventRecord(event=event, harm_fields=fields, dispositions=dispositions)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _ledger_payload(
    event: Event,
    disposition: Disposition,
    confirmations: dict[HarmFieldName, FieldConfirmation],
) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "seq": disposition.seq,
        "tier": disposition.tier,
        "previous_tier": disposition.previous_tier,
        "rationale": disposition.rationale,
        "state": event.state,
        "recorded_at": _iso(disposition.recorded_at),
        "deadline_utc": _iso(disposition.deadline_utc) if disposition.deadline_utc else None,
        "harm_fields": {
            name.value: {"status": c.status.value, "evidence": c.evidence}
            for name, c in sorted(
                confirmations.items(), key=lambda item: list(HarmFieldName).index(item[0])
            )
        },
    }
