"""Unit of work: one database transaction plus the repositories that run inside it.

Services do all their writes inside `with uow:`, together with ledger and notification calls
that receive `uow.session`. Leaving the block normally commits; any exception (including
UnimplementedError from an unimplemented integration) rolls everything back (Phase 2, Q7.2
Decision 1).

Teammates add their repositories to `UnitOfWork` and `SqlAlchemyUnitOfWork`.
"""

from types import TracebackType
from typing import Protocol, Self

from sqlalchemy.orm import Session, sessionmaker

from app.modules.auth.repository import AuthRepository, SqlAuthRepository
from app.modules.events.repository import EventRepository, SqlEventRepository
from app.modules.ledger.repository import LedgerRepository, SqlLedgerRepository
from app.modules.metrics.repository import MetricRepository, SqlMetricRepository
from app.modules.notifications.repository import (
    NotificationRepository,
    SqlNotificationRepository,
    SqlWebhookRepository,
    WebhookRepository,
)
from app.modules.systems.repository import SqlSystemRepository, SystemRepository
from app.modules.thresholds.repository import SqlThresholdRepository, ThresholdRepository


class UnitOfWork(Protocol):
    # The active SQLAlchemy session, passed to ledger/notification writers. None in test doubles.
    @property
    def session(self) -> Session | None: ...
    @property
    def systems(self) -> SystemRepository: ...
    @property
    def metrics(self) -> MetricRepository: ...
    @property
    def thresholds(self) -> ThresholdRepository: ...
    @property
    def events(self) -> EventRepository: ...
    @property
    def auth(self) -> AuthRepository: ...
    # Read side only; entries are written through the LedgerWriter.
    @property
    def ledger(self) -> LedgerRepository: ...
    @property
    def notifications(self) -> NotificationRepository: ...
    @property
    def webhooks(self) -> WebhookRepository: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


class SqlAlchemyUnitOfWork:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self.session: Session | None = None

    def __enter__(self) -> Self:
        session = self._session_factory()
        session.begin()
        self.session = session
        self.systems: SystemRepository = SqlSystemRepository(session)
        self.metrics: MetricRepository = SqlMetricRepository(session)
        self.thresholds: ThresholdRepository = SqlThresholdRepository(session)
        self.events: EventRepository = SqlEventRepository(session)
        self.auth: AuthRepository = SqlAuthRepository(session)
        self.ledger: LedgerRepository = SqlLedgerRepository(session)
        self.notifications: NotificationRepository = SqlNotificationRepository(session)
        self.webhooks: WebhookRepository = SqlWebhookRepository(session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self.session is None:
            raise RuntimeError("unit of work exited without being entered")
        try:
            if exc_type is None:
                self.session.commit()
            else:
                self.session.rollback()
        finally:
            self.session.close()
            self.session = None


def get_uow() -> UnitOfWork:
    """FastAPI dependency: a fresh unit of work per request (no connection until entered)."""
    from app.db.session import get_sessionmaker

    return SqlAlchemyUnitOfWork(get_sessionmaker())
