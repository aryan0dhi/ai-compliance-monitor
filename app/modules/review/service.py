"""Harm fields of a flagged breach (REQ-07, B-07, D-20).

`open_harm_fields` runs inside the flagging transaction: every breach event gets all four Art.
3(49) fields as PENDING. A field its system lists (monitored_system.harm_fields) is prefilled with
evidence taken from the event's own breach record; the others stay null. Nothing here assigns a
severity; the reviewer confirms each field (REQ-08a).
"""

from datetime import UTC

from app.db.uow import UnitOfWork
from app.modules.events.models import Event
from app.modules.review.models import HarmField, HarmFieldName, HarmFieldStatus
from app.modules.systems.models import MonitoredSystem
from app.modules.thresholds.models import ThresholdVersion


def breach_evidence(event: Event, threshold: ThresholdVersion) -> str:
    """The breach facts as one sentence, built only from stored values (never guessed)."""
    if event.metric_timestamp is None:  # pragma: no cover - breach events always have one
        raise ValueError("not a threshold breach")
    when = event.metric_timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return (
        f"{event.metric_name} was {event.observed_value} at {when}, "
        f"{threshold.direction} the threshold of {event.threshold_value} "
        f"(threshold version {event.threshold_version_id})."
    )


def open_harm_fields(
    uow: UnitOfWork, *, event: Event, system: MonitoredSystem, threshold: ThresholdVersion
) -> list[HarmField]:
    """Create the four PENDING harm fields of a new breach event, in the caller's transaction."""
    listed = set(system.harm_fields or ())  # None on a system object not yet flushed
    evidence = breach_evidence(event, threshold)
    fields = [
        HarmField(
            event_id=event.id,
            name=name.value,
            status=HarmFieldStatus.PENDING.value,
            prefill_evidence=evidence if name.value in listed else None,
        )
        for name in HarmFieldName
    ]
    uow.harm_fields.add_all(fields)
    return fields
