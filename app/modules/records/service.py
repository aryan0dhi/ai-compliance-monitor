"""Sealing, revealing and erasing an event's personal data (REQ-16; REQ-05; AC-18; D-28).

What counts as personal data: free text a person enters about an event, which may name people or
describe harm to them. That is the disposition rationale, the evidence a reviewer gives when
confirming a harm field, the report submission note, and the assembled report (which quotes the
rationale). Machine-generated values (metric values, prefilled breach evidence, ids, timestamps)
are not sealed. Actor ids are pseudonymous and stay readable so the audit trail keeps working.

Erasure: once the service clock reaches `erasure_due_at(event)` the worker destroys the event's
key in one transaction with a PERSONAL_DATA_ERASED ledger entry. Sealed values then read as null,
and the event refuses new dispositions and report reads (409).
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import UUID

from app.core.clock import Clock
from app.core.config import AppEnv, get_settings
from app.core.errors import ErrorCode
from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event, retain_until_for
from app.modules.ledger.interface import LedgerEntryType, LedgerWriter
from app.modules.records.crypto import (
    DEVELOPMENT_MASTER_KEY,
    is_token,
    new_data_key,
    open_token,
    parse_master_key,
    seal,
    unwrap_key,
    wrap_key,
)
from app.modules.records.models import RecordKey
from app.modules.review.models import Disposition, HarmField

logger = logging.getLogger(__name__)


def rationale_purpose(seq: int) -> str:
    return f"disposition.rationale:{seq}"


def evidence_purpose(field_name: str) -> str:
    return f"harm_field.evidence:{field_name}"


def report_purpose(disposition_seq: int) -> str:
    return f"report.document:{disposition_seq}"


def note_purpose(disposition_seq: int) -> str:
    return f"report.submission_note:{disposition_seq}"


class PersonalDataErased(Exception):
    """The event's key was destroyed, so nothing personal can be written or read any more."""


@lru_cache
def master_key() -> bytes:
    """RECORD_KEY_MASTER_KEY, or the public development key outside production."""
    settings = get_settings()
    if settings.record_key_master_key is not None:
        return parse_master_key(settings.record_key_master_key.get_secret_value())
    if settings.app_env == AppEnv.PRODUCTION:
        raise RuntimeError("RECORD_KEY_MASTER_KEY is required when APP_ENV=production")
    logger.warning(
        "RECORD_KEY_MASTER_KEY is not set: personal data is sealed with the public development "
        "key, which protects nothing (REQ-16). Set it in any shared deployment."
    )
    return DEVELOPMENT_MASTER_KEY


@dataclass
class Vault:
    """One event's personal data, inside the caller's unit of work."""

    event_id: UUID
    data_key: bytes | None
    erased_at: datetime | None = None

    def seal(self, purpose: str, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        if self.data_key is None:
            raise PersonalDataErased(f"personal data of event {self.event_id} has been erased")
        return seal(self.data_key, self.event_id, purpose, plaintext)

    def reveal(self, purpose: str, stored: str | None) -> str | None:
        """The plaintext, or None once erased. A value that isn't a token is returned as is
        (rows written before migration 0012 that it could not reach)."""
        if stored is None or not is_token(stored):
            return stored
        if self.data_key is None:
            return None
        return open_token(self.data_key, self.event_id, purpose, stored)


def open_vault(uow: UnitOfWork, event_id: UUID, *, create_at: datetime | None = None) -> Vault:
    """The event's vault. With `create_at`, a key is created (at that time) if it has none yet;
    without, an event with no key gets an empty vault that seals nothing."""
    key = uow.record_keys.get(event_id)
    if key is None:
        if create_at is None:
            return Vault(event_id=event_id, data_key=None)
        data_key = new_data_key()
        uow.record_keys.add(
            RecordKey(
                event_id=event_id,
                wrapped_key=wrap_key(master_key(), event_id, data_key),
                created_at=create_at,
            )
        )
        return Vault(event_id=event_id, data_key=data_key)
    if key.wrapped_key is None:
        return Vault(event_id=event_id, data_key=None, erased_at=key.destroyed_at)
    return Vault(event_id=event_id, data_key=unwrap_key(master_key(), event_id, key.wrapped_key))


def revealed_rationales(vault: Vault, dispositions: list[Disposition]) -> dict[int, str | None]:
    return {d.seq: vault.reveal(rationale_purpose(d.seq), d.rationale) for d in dispositions}


def revealed_evidence(vault: Vault, fields: list[HarmField]) -> dict[str, str | None]:
    """Each harm field's reviewer evidence in plaintext, by name; null once erased."""
    return {f.name: vault.reveal(evidence_purpose(f.name), f.evidence) for f in fields}


def erasure_due_at(event: Event) -> datetime:
    """When the event's personal data is erased: once its retention period has ended (REQ-05,
    REQ-16), and never before detected_at + 6 calendar months + 1 day, so AC-05's literal
    5mo29d check always finds the record intact whatever D-18 decides."""
    return max(
        event.retain_until.astimezone(UTC),
        retain_until_for(event.detected_at) + timedelta(days=1),
    )


@dataclass
class ErasureResult:
    erased: list[UUID] = field(default_factory=list)


# What erasure makes unreadable, as reported in `erased_fields` (contract delta \u03946, D-29).
PERSONAL_FIELDS = (
    "disposition.rationale",
    "harm_fields.evidence",
    "report.document",
    "report.submission_note",
)


class UnknownEvent(Exception):
    """No event with this id (404)."""


class ErasureDenied(Exception):
    """Only reviewers erase on request (403, contract delta \u03946)."""


class RetentionNotExpired(Exception):
    """A request to erase before retain_until (409 RETENTION_NOT_EXPIRED)."""

    error_code = ErrorCode.RETENTION_NOT_EXPIRED


class AlreadyErased(Exception):
    """The event's personal data was erased already (409 ALREADY_ERASED)."""

    error_code = ErrorCode.ALREADY_ERASED

    def __init__(self) -> None:
        super().__init__("the personal data of this event has been erased")


@dataclass(frozen=True)
class Erasure:
    event_id: UUID
    erased_at: datetime
    erased_fields: list[str]


def personal_fields_present(uow: UnitOfWork, event_id: UUID) -> list[str]:
    """Which PERSONAL_FIELDS hold anything for this event, in PERSONAL_FIELDS order."""
    dispositions = uow.dispositions.for_event(event_id)
    reports = [r for d in dispositions if (r := uow.reports.get(event_id, d.seq)) is not None]
    present = {
        "disposition.rationale": any(d.rationale is not None for d in dispositions),
        "harm_fields.evidence": any(
            f.evidence is not None for f in uow.harm_fields.for_event(event_id)
        ),
        "report.document": bool(reports),
        "report.submission_note": any(r.submission_note is not None for r in reports),
    }
    return [name for name in PERSONAL_FIELDS if present[name]]


def _destroy(
    uow: UnitOfWork,
    event: Event,
    key: RecordKey | None,
    *,
    now: datetime,
    trigger: str,
    actor_id: UUID | None,
    ledger: LedgerWriter,
) -> list[str]:
    """Destroy the event's key (creating an already-destroyed one if it never had personal
    data), with one RECORD_ERASED ledger entry. Runs in the caller's locked transaction."""
    fields = personal_fields_present(uow, event.id)
    if key is None:
        uow.record_keys.add(
            RecordKey(event_id=event.id, wrapped_key=None, created_at=now, destroyed_at=now)
        )
    else:
        key.wrapped_key = None
        key.destroyed_at = now
    ledger.append(
        uow.session,
        entry_type=LedgerEntryType.RECORD_ERASED,
        actor_id=actor_id,
        payload={
            "event_id": str(event.id),
            "retain_until": event.retain_until.astimezone(UTC).isoformat(),
            "erased_at": now.astimezone(UTC).isoformat(),
            "trigger": trigger,
            "erased_fields": fields,
        },
        event_id=event.id,
    )
    return fields


def authorize_erasure(principal: Principal) -> None:
    if principal.role is not Role.REVIEWER:
        raise ErasureDenied(f"role {principal.role} may not erase records")


def erase_on_request(
    event_id: UUID,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    clock: Clock,
) -> Erasure:
    """POST /v1/events/{id}/erasure (contract delta \u03946): a reviewer erases the event's
    personal data now, once its retain_until has passed. Raises ErasureDenied (403),
    UnknownEvent (404), AlreadyErased or RetentionNotExpired (409); nothing changes then."""
    authorize_erasure(principal)
    with uow:
        event = uow.events.get_for_update(event_id)
        if event is None:
            raise UnknownEvent(str(event_id))
        key = uow.record_keys.get(event_id)
        if key is not None and key.wrapped_key is None:
            raise AlreadyErased()
        now = clock.now()
        if now < event.retain_until:
            raise RetentionNotExpired(
                "the event is retained until "
                f"{event.retain_until.astimezone(UTC).isoformat()} (REQ-05)"
            )
        fields = _destroy(
            uow,
            event,
            key,
            now=now,
            trigger="MANUAL",
            actor_id=principal.actor_id,
            ledger=ledger,
        )
    return Erasure(event_id=event_id, erased_at=now, erased_fields=fields)


def erase_if_due(uow: UnitOfWork, event_id: UUID, *, ledger: LedgerWriter, clock: Clock) -> bool:
    """Destroy the event's key if its erasure is due. One transaction with its ledger entry."""
    with uow:
        # Locked like a disposition or report, so nothing is sealed under a key being destroyed.
        event = uow.events.get_for_update(event_id)
        key = uow.record_keys.get(event_id)
        if event is None or key is None or key.wrapped_key is None:
            return False
        now = clock.now()
        if now < erasure_due_at(event):
            return False
        _destroy(uow, event, key, now=now, trigger="RETENTION", actor_id=None, ledger=ledger)
    return True


def erase_expired(
    uow_factory: Callable[[], UnitOfWork], *, ledger: LedgerWriter, clock: Clock
) -> ErasureResult:
    """One worker pass: erase every event whose retention has ended (one transaction each)."""
    result = ErasureResult()
    with uow_factory() as uow:
        candidates = uow.record_keys.live_past_retention(clock.now())
    for event_id in candidates:
        if erase_if_due(uow_factory(), event_id, ledger=ledger, clock=clock):
            result.erased.append(event_id)
    return result
