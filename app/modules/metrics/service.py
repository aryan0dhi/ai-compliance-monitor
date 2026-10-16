"""Metric ingestion (REQ-01) with threshold detection (REQ-03).

`ingest_metric` is framework-independent: it takes plain values and explicit collaborators
(unit of work, ledger, notifier, clock), so it can be reused by other entry points such as an
Evidently adapter or test fixtures.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.core.clock import Clock
from app.db.uow import UnitOfWork
from app.modules.auth.interface import Principal, Role
from app.modules.events.service import create_breach_event
from app.modules.ledger.interface import LedgerWriter
from app.modules.metrics.models import MetricSample, MetricType
from app.modules.notifications.interface import Notifier
from app.modules.thresholds.service import is_breach, threshold_in_force


class MetricIngestError(Exception):
    pass


class SystemScopeError(MetricIngestError):
    """The caller is not a monitored-system key scoped to the submitted system_id (403)."""


class UnknownSystemError(MetricIngestError):
    """No monitored_system row for the submitted system_id."""


class MetricTypeMismatchError(MetricIngestError):
    """The submitted metric_type differs from the configured threshold's metric_type (D-06)."""


class ConflictingDuplicateError(MetricIngestError):
    """TEMPORARY(D-04): same (system_id, metric_name, timestamp) already stored, different data."""


@dataclass(frozen=True)
class MetricSubmission:
    system_id: UUID
    metric_name: str
    metric_type: MetricType
    value: Decimal
    sampled_at: datetime  # the API's `timestamp`, timezone-aware


@dataclass(frozen=True)
class IngestResult:
    sample: MetricSample
    flagged_event_id: UUID | None
    # False when the submission repeated an already stored sample (TEMPORARY(D-04)).
    created: bool


def authorize_submission(principal: Principal, system_id: UUID) -> None:
    """Section 5.1: metric submission uses a monitored-system key scoped to that system_id."""
    if principal.role is not Role.SYSTEM or principal.system_id != system_id:
        raise SystemScopeError("API key is not scoped to this system_id")


def ingest_metric(
    submission: MetricSubmission,
    *,
    principal: Principal,
    uow: UnitOfWork,
    ledger: LedgerWriter,
    notifier: Notifier,
    clock: Clock,
) -> IngestResult:
    """Store a metric sample and flag an event if it breaches the threshold in force.

    Everything happens in one transaction: the sample, the system's last_seen_at, and (on a
    breach) the event, its ledger entry and its notification. If any step fails, nothing is
    stored.
    """
    authorize_submission(principal, submission.system_id)

    with uow:
        system = uow.systems.get(submission.system_id)
        if system is None:
            raise UnknownSystemError(str(submission.system_id))

        threshold = threshold_in_force(
            uow.thresholds, submission.system_id, submission.metric_name, submission.sampled_at
        )
        if threshold is not None and threshold.metric_type != submission.metric_type:
            raise MetricTypeMismatchError(
                f"{submission.metric_name} is configured as {threshold.metric_type}, "
                f"got {submission.metric_type}"
            )

        now = clock.now()
        sample = MetricSample(
            system_id=submission.system_id,
            metric_name=submission.metric_name,
            sampled_at=submission.sampled_at,
            metric_type=submission.metric_type.value,
            value=submission.value,
            received_at=now,
        )
        if not uow.metrics.add_if_absent(sample):
            return _resolve_duplicate(uow, submission)

        uow.systems.touch_last_seen(system, now)

        flagged_event_id = None
        if threshold is not None and is_breach(threshold, submission.value):
            event = create_breach_event(
                uow,
                sample=sample,
                threshold=threshold,
                detected_at=now,
                actor_id=principal.actor_id,
                ledger=ledger,
                notifier=notifier,
            )
            flagged_event_id = event.id

    return IngestResult(sample=sample, flagged_event_id=flagged_event_id, created=True)


def _resolve_duplicate(uow: UnitOfWork, submission: MetricSubmission) -> IngestResult:
    """TEMPORARY(D-04): behavior for a repeated sample key until the pod agrees on it.

    - Same metric_type and value: return the stored sample and its event (if any); nothing new
      is created and last_seen_at is not updated.
    - Different metric_type or value: reject; the stored sample is never overwritten.
    """
    existing = uow.metrics.get(submission.system_id, submission.metric_name, submission.sampled_at)
    if existing is None:  # pragma: no cover - would mean add_if_absent lied
        raise RuntimeError("duplicate sample reported but not found")
    if existing.metric_type != submission.metric_type or existing.value != submission.value:
        raise ConflictingDuplicateError(
            "a different sample is already stored for this system_id, metric_name and timestamp"
        )
    event = uow.events.find_breach_for_sample(
        existing.system_id, existing.metric_name, existing.sampled_at
    )
    return IngestResult(
        sample=existing, flagged_event_id=event.id if event else None, created=False
    )
