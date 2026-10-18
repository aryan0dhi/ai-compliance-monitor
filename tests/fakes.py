"""Explicit test doubles. Used ONLY by tests; production code never imports this module.

- FakeUnitOfWork: in-memory repositories with commit/rollback semantics (containers AND the
  column attributes of the objects in them are restored when the `with` block raises), so
  transaction-atomicity rules can be checked without PostgreSQL.
- RecordingLedger / RecordingNotifier: record calls and can be told to fail.
- ChainLedger: the real hash-chain rules over the fake ledger repository, for verify/export tests.
- OutboxRecorder: the real outbox row builder writing into the fake notification repository.
- RecordingSender: a webhook sender that records requests and returns scripted results.
- FixedClock: deterministic time.
"""

import copy
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy.orm import object_mapper

from app.modules.auth.models import ApiKey, AppUser
from app.modules.auth.principal import Principal, Role
from app.modules.events.models import OPEN_GAP_STATES, Event, EventType
from app.modules.events.repository import EventFilters
from app.modules.ledger.chain import GENESIS_HASH, compute_entry_hash
from app.modules.ledger.models import LedgerEntry, LedgerEntryType
from app.modules.metrics.models import MetricSample
from app.modules.notifications.delivery import SendResult
from app.modules.notifications.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    Webhook,
)
from app.modules.notifications.outbox import build_notification
from app.modules.systems.models import MonitoredSystem
from app.modules.thresholds.models import ThresholdVersion

# --- repositories -------------------------------------------------------------------------------


class FakeSystemRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, MonitoredSystem] = {}

    def get(self, system_id: UUID) -> MonitoredSystem | None:
        return self.rows.get(system_id)

    def get_for_update(self, system_id: UUID) -> MonitoredSystem | None:
        return self.rows.get(system_id)

    def list_reporting(self) -> list[MonitoredSystem]:
        return [s for s in self.rows.values() if s.last_seen_at is not None]

    def add(self, system: MonitoredSystem) -> None:
        self.rows[system.id] = system

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

    def first_received_after(self, system_id: UUID, after: datetime) -> datetime | None:
        times = [
            s.received_at
            for s in self.rows.values()
            if s.system_id == system_id and s.received_at > after
        ]
        return min(times, default=None)


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

    def get_for_update(self, event_id: UUID) -> Event | None:
        return self.rows.get(event_id)

    def open_gaps(self) -> list[Event]:
        gaps = [
            e
            for e in self.rows.values()
            if e.type == EventType.MONITORING_GAP and e.state in OPEN_GAP_STATES
        ]
        return sorted(gaps, key=lambda e: (e.detected_at, e.id))

    def open_gap_for_system(self, system_id: UUID) -> Event | None:
        return next((e for e in self.open_gaps() if e.system_id == system_id), None)

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


class FakeAuthRepository:
    def __init__(self) -> None:
        self.users: dict[UUID, AppUser] = {}
        self.keys: dict[UUID, ApiKey] = {}

    def principal_for_key(self, key_hash: str) -> Principal | None:
        for key in self.keys.values():
            if key.key_hash == key_hash and key.revoked_at is None:
                user = self.users[key.user_id]
                return Principal(actor_id=user.id, role=Role(user.role), system_id=user.system_id)
        return None

    def get_user_by_name(self, name: str) -> AppUser | None:
        return next((u for u in self.users.values() if u.name == name), None)

    def get_key(self, key_id: UUID) -> ApiKey | None:
        return self.keys.get(key_id)

    def add_user(self, user: AppUser) -> None:
        self.users[user.id] = user

    def add_key(self, key: ApiKey) -> None:
        self.keys[key.id] = key


class FakeLedgerRepository:
    def __init__(self) -> None:
        self.entries: list[LedgerEntry] = []

    def page(self, *, after_seq: int, limit: int) -> list[LedgerEntry]:
        return [e for e in sorted(self.entries, key=lambda e: e.seq) if e.seq > after_seq][:limit]

    def for_event(self, event_id: UUID) -> list[LedgerEntry]:
        return sorted((e for e in self.entries if e.event_id == event_id), key=lambda e: e.seq)


class FakeWebhookRepository:
    def __init__(self) -> None:
        self.rows: dict[UUID, Webhook] = {}

    def add(self, webhook: Webhook) -> None:
        self.rows[webhook.id] = webhook

    def active(self) -> list[Webhook]:
        hooks = [w for w in self.rows.values() if w.disabled_at is None]
        return sorted(hooks, key=lambda w: w.created_at)


class FakeNotificationRepository:
    def __init__(self, webhooks: FakeWebhookRepository) -> None:
        self._webhooks = webhooks
        self.rows: dict[UUID, Notification] = {}
        self.deliveries: dict[tuple[UUID, UUID], NotificationDelivery] = {}

    def add(self, notification: Notification) -> None:
        self.rows[notification.id] = notification

    def get(self, notification_id: UUID) -> Notification | None:
        return self.rows.get(notification_id)

    def undispatched(self, limit: int) -> list[Notification]:
        pending = [n for n in self.rows.values() if n.dispatched_at is None]
        return sorted(pending, key=lambda n: (n.created_at, n.id))[:limit]

    def add_delivery(self, delivery: NotificationDelivery) -> None:
        self.deliveries[(delivery.notification_id, delivery.webhook_id)] = delivery

    def get_delivery(self, notification_id: UUID, webhook_id: UUID) -> NotificationDelivery | None:
        return self.deliveries.get((notification_id, webhook_id))

    def due_deliveries(
        self, now: datetime, limit: int
    ) -> list[tuple[NotificationDelivery, Notification, Webhook]]:
        due = []
        for d in self.deliveries.values():
            webhook = self._webhooks.rows[d.webhook_id]
            if (
                d.status == DeliveryStatus.PENDING
                and d.next_attempt_at <= now
                and webhook.disabled_at is None
            ):
                due.append((d, self.rows[d.notification_id], webhook))
        due.sort(key=lambda t: (t[1].deliver_by, t[0].next_attempt_at))
        return due[:limit]

    def count_undelivered_due_between(self, after: datetime, until: datetime) -> int:
        delivered = {
            d.notification_id
            for d in self.deliveries.values()
            if d.status == DeliveryStatus.DELIVERED
        }
        return sum(
            1 for n in self.rows.values() if after < n.deliver_by <= until and n.id not in delivered
        )


def _column_state(obj: object) -> dict[str, Any]:
    return {a.key: getattr(obj, a.key) for a in object_mapper(obj).column_attrs}


class FakeUnitOfWork:
    """In-memory unit of work. Commits on clean exit, restores prior state on exception."""

    def __init__(self) -> None:
        self.session = None
        self.systems = FakeSystemRepository()
        self.metrics = FakeMetricRepository()
        self.thresholds = FakeThresholdRepository()
        self.events = FakeEventRepository()
        self.auth = FakeAuthRepository()
        self.ledger = FakeLedgerRepository()
        self.webhooks = FakeWebhookRepository()
        self.notifications = FakeNotificationRepository(self.webhooks)
        self.commits = 0
        self.rollbacks = 0

    def _repositories(self) -> list[object]:
        return [
            self.systems,
            self.metrics,
            self.thresholds,
            self.events,
            self.auth,
            self.ledger,
            self.webhooks,
            self.notifications,
        ]

    def __enter__(self) -> "FakeUnitOfWork":
        self._snapshot = []
        for repo in self._repositories():
            for name, container in vars(repo).items():
                if isinstance(container, dict | list):
                    items = container.values() if isinstance(container, dict) else container
                    states = [(obj, _column_state(obj)) for obj in items]
                    self._snapshot.append((repo, name, copy.copy(container), states))
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commits += 1
            return
        for repo, name, container, states in self._snapshot:
            setattr(repo, name, container)
            for obj, state in states:
                for key, value in state.items():
                    setattr(obj, key, value)
        self.rollbacks += 1

    # Seeding helpers (fixtures write directly, outside any transaction).
    def seed_system(
        self,
        system_id: UUID | None = None,
        name: str = "loan-scorer",
        *,
        sampling_interval_s: int = 300,
        gap_missed_intervals: int = 3,
        last_seen_at: datetime | None = None,
    ) -> UUID:
        system_id = system_id or uuid4()
        self.systems.rows[system_id] = MonitoredSystem(
            id=system_id,
            name=name,
            sampling_interval_s=sampling_interval_s,
            gap_missed_intervals=gap_missed_intervals,
            last_seen_at=last_seen_at,
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

    def seed_webhook(
        self,
        url: str = "https://hooks.example.com/reviewer",
        *,
        event_types: tuple[str, ...] = ("flag_notification", "reminder"),
        created_at: datetime | None = None,
    ) -> Webhook:
        webhook = Webhook(
            id=uuid4(),
            url=url,
            event_types=list(event_types),
            secret="test-secret",
            created_by=None,
            created_at=created_at or datetime(2026, 1, 1, tzinfo=UTC),
            disabled_at=None,
        )
        self.webhooks.rows[webhook.id] = webhook
        return webhook


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
class ChainLedger:
    """LedgerWriter applying the production hash-chain rules to a FakeLedgerRepository."""

    repo: FakeLedgerRepository
    clock: "Clock"

    def append(self, session, *, entry_type, actor_id, payload, event_id=None) -> None:
        head = max(self.repo.entries, key=lambda e: e.seq, default=None)
        seq = head.seq + 1 if head else 1
        prev_hash = head.entry_hash if head else GENESIS_HASH
        at = self.clock.now()
        entry_type = LedgerEntryType(entry_type).value
        self.repo.entries.append(
            LedgerEntry(
                seq=seq,
                entry_type=entry_type,
                event_id=event_id,
                actor_id=actor_id,
                at=at,
                payload=payload,
                prev_hash=prev_hash,
                entry_hash=compute_entry_hash(
                    seq=seq,
                    entry_type=entry_type,
                    event_id=event_id,
                    actor_id=actor_id,
                    at=at,
                    payload=payload,
                    prev_hash=prev_hash,
                ),
            )
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
class OutboxRecorder:
    """Notifier that writes real outbox rows into a FakeNotificationRepository."""

    repo: FakeNotificationRepository
    clock: "Clock"
    max_delay: timedelta = timedelta(minutes=15)

    def enqueue(self, session, *, notification_type, event_id, payload) -> None:
        self.repo.add(
            build_notification(
                event_id=event_id,
                notification_type=notification_type,
                payload=payload,
                now=self.clock.now(),
                max_delay=self.max_delay,
            )
        )


@dataclass
class RecordingSender:
    """Webhook sender double. `respond(url)` scripts each result (default: 200 OK)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    respond: Callable[[str], SendResult] | None = None

    def send(self, url: str, body: bytes, secret: str) -> SendResult:
        self.calls.append({"url": url, "body": body, "secret": secret})
        if self.respond is not None:
            return self.respond(url)
        return SendResult(ok=True, status_code=200, error=None)


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
    from app.modules.notifications.delivery import WebhookSender
    from app.modules.notifications.interface import Notifier

    _uow: UnitOfWork = FakeUnitOfWork()
    _ledger: LedgerWriter = RecordingLedger()
    _chain: LedgerWriter = ChainLedger(FakeLedgerRepository(), FixedClock(datetime.now()))
    _notifier: Notifier = RecordingNotifier()
    _outbox: Notifier = OutboxRecorder(
        FakeNotificationRepository(FakeWebhookRepository()), FixedClock(datetime.now())
    )
    _sender: WebhookSender = RecordingSender()
    _clock: Clock = FixedClock(datetime.now())
