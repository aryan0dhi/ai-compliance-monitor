"""Explicit test doubles. Used ONLY by tests; production code never imports this module.

- FakeUnitOfWork: in-memory repositories with commit/rollback semantics (state is restored
  when the `with` block raises), so transaction-atomicity rules can be checked without
  PostgreSQL.
- RecordingLedger / RecordingNotifier: stand-ins for the unimplemented ledger and notifications
  modules. They record calls and can be told to fail.
- FixedClock: deterministic time.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from app.modules.auth.interface import Principal, Role
from app.modules.events.models import Event
from app.modules.events.repository import EventFilters
from app.modules.metrics.models import MetricSample
from app.modules.systems.models import MonitoredSystem
from app.modules.thresholds.models import ThresholdVersion

# --- repositories -------------------------------------------------------------------------------


class FakeSystemRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, MonitoredSystem] = {}

    def get(self, system_id: UUID) -> MonitoredSystem | None:
        return self.rows.get(system_id)

    def touch_last_seen(self, system: MonitoredSystem, at: datetime) -> None:
        system.last_seen_at = at


class FakeMetricRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str, datetime], MetricSample] = {}

    def get(self, system_id: UUID, metric_name: str, sampled_at: datetime) -> MetricSample | None:
        return self.rows.get((system_id, metric_name, sampled_at))

    def add_if_absent(self, sample: MetricSample) -> bool:
        key = (sample.system_id, sample.metric_name, sample.sampled_at)
        if key in self.rows:
            return False
        self.rows[key] = sample
        return True


class FakeThresholdRepository:
    def __init__(self) -> None:
        self.rows: list[ThresholdVersion] = []

    def in_force(self, system_id: UUID, metric_name: str, at: datetime) -> ThresholdVersion | None:
        candidates = [
            t
            for t in self.rows
            if t.system_id == system_id and t.metric_name == metric_name and t.valid_from <= at
        ]
        return max(candidates, key=lambda t: t.valid_from, default=None)

    def add(self, threshold: ThresholdVersion) -> None:
        self.rows.append(threshold)


class FakeEventRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, Event] = {}

    def add(self, event: Event) -> None:
        self.rows[event.id] = event

    def get(self, event_id: UUID) -> Event | None:
        return self.rows.get(event_id)

    def find_breach_for_sample(
        self, system_id: UUID, metric_name: str, metric_timestamp: datetime
    ) -> Event | None:
        for e in self.rows.values():
            if (
                e.type == "threshold_breach"
                and e.system_id == system_id
                and e.metric_name == metric_name
                and e.metric_timestamp == metric_timestamp
            ):
                return e
        return None

    def list(self, filters: EventFilters, *, limit: int, offset: int) -> tuple[list[Event], int]:
        matches = [
            e
            for e in self.rows.values()
            if (filters.system_id is None or e.system_id == filters.system_id)
            and (filters.state is None or e.state == filters.state.value)
            and (filters.type is None or e.type == filters.type.value)
            and (filters.since is None or e.detected_at >= filters.since)
        ]
        matches.sort(key=lambda e: (e.detected_at, e.id), reverse=True)
        return matches[offset : offset + limit], len(matches)


class FakeUnitOfWork:
    """In-memory unit of work. Commits on clean exit, restores prior state on exception."""

    def __init__(self) -> None:
        self.session = None
        self.systems = FakeSystemRepository()
        self.metrics = FakeMetricRepository()
        self.thresholds = FakeThresholdRepository()
        self.events = FakeEventRepository()
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> "FakeUnitOfWork":
        self._snapshot = (
            dict(self.metrics.rows),
            dict(self.events.rows),
            {sid: s.last_seen_at for sid, s in self.systems.rows.items()},
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commits += 1
            return
        metrics, events, last_seen = self._snapshot
        self.metrics.rows = metrics
        self.events.rows = events
        for sid, value in last_seen.items():
            self.systems.rows[sid].last_seen_at = value
        self.rollbacks += 1

    # Seeding helpers (fixtures write directly, outside any transaction).
    def seed_system(self, system_id: UUID | None = None, name: str = "loan-scorer") -> UUID:
        system_id = system_id or uuid4()
        self.systems.rows[system_id] = MonitoredSystem(
            id=system_id, name=name, sampling_interval_s=300
        )
        return system_id

    def seed_threshold(
        self,
        system_id: UUID,
        metric_name: str,
        *,
        metric_type: str,
        direction: str,
        limit: str,
        valid_from: datetime,
    ) -> ThresholdVersion:
        from decimal import Decimal

        t = ThresholdVersion(
            id=uuid4(),
            system_id=system_id,
            metric_name=metric_name,
            metric_type=metric_type,
            direction=direction,
            limit_value=Decimal(limit),
            valid_from=valid_from,
        )
        self.thresholds.rows.append(t)
        return t


# --- integrations ------------------------------------------------------------------------------


@dataclass
class RecordingLedger:
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Exception | None = None

    def append(self, session, *, entry_type, actor_id, payload, event_id=None) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(
            {
                "session": session,
                "entry_type": entry_type,
                "actor_id": actor_id,
                "payload": payload,
                "event_id": event_id,
            }
        )


@dataclass
class RecordingNotifier:
    calls: list[dict[str, Any]] = field(default_factory=list)
    fail_with: Exception | None = None

    def enqueue(self, session, *, notification_type, event_id, payload) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.calls.append(
            {
                "session": session,
                "notification_type": notification_type,
                "event_id": event_id,
                "payload": payload,
            }
        )


@dataclass
class FixedClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


# --- principals --------------------------------------------------------------------------------


def system_principal(system_id: UUID) -> Principal:
    return Principal(actor_id=uuid4(), role=Role.SYSTEM, system_id=system_id)


def user_principal(role: Role) -> Principal:
    return Principal(actor_id=uuid4(), role=role)


# Static conformance: mypy fails if a double drifts from the production Protocol it replaces.
if TYPE_CHECKING:
    from app.core.clock import Clock
    from app.db.uow import UnitOfWork
    from app.modules.ledger.interface import LedgerWriter
    from app.modules.notifications.interface import Notifier

    _uow: UnitOfWork = FakeUnitOfWork()
    _ledger: LedgerWriter = RecordingLedger()
    _notifier: Notifier = RecordingNotifier()
    _clock: Clock = FixedClock(datetime.now())
