"""The worker loop. One active instance at a time (session advisory lock, Q7.2)."""

import logging
import os
import signal
import socket
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import Clock, SystemClock, get_clock
from app.db.uow import SqlAlchemyUnitOfWork, UnitOfWork
from app.modules.ledger.interface import LedgerWriter, get_ledger
from app.modules.monitoring_gaps.service import GapSweepResult, sweep_gaps
from app.modules.notifications.delivery import WebhookSender, sender_from_settings
from app.modules.notifications.dispatcher import DispatchResult, dispatch_due
from app.modules.notifications.interface import Notifier, get_notifier
from app.modules.worker.models import WorkerHeartbeat

logger = logging.getLogger(__name__)

# pg_try_advisory_lock key held for the worker's lifetime ("WORK").
WORKER_LOCK_KEY = 0x574F524B
# On start, report notifications that went overdue this long before (e.g. while no worker ran).
OVERDUE_LOOKBACK = timedelta(days=1)


@dataclass
class TickResult:
    gaps: GapSweepResult | None = None
    dispatch: DispatchResult | None = None
    newly_overdue: int = 0
    errors: list[str] = field(default_factory=list)


class Worker:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], UnitOfWork],
        ledger: LedgerWriter,
        notifier: Notifier,
        sender: WebhookSender,
        clock_provider: Callable[[], Clock],
        max_attempts: int,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._ledger = ledger
        self._notifier = notifier
        self._sender = sender
        self._clock_provider = clock_provider
        self._max_attempts = max_attempts
        self._heartbeat = heartbeat
        self._overdue_checked_until: datetime | None = None

    def run_once(self) -> TickResult:
        """One pass. Each step is isolated: a failure is logged and the others still run."""
        clock = self._clock_provider()
        result = TickResult()
        try:
            result.gaps = sweep_gaps(
                self._uow_factory, ledger=self._ledger, notifier=self._notifier, clock=clock
            )
        except Exception:
            logger.exception("gap sweep failed")
            result.errors.append("gaps")
        try:
            result.dispatch = dispatch_due(
                self._uow_factory,
                sender=self._sender,
                ledger=self._ledger,
                clock=clock,
                max_attempts=self._max_attempts,
            )
        except Exception:
            logger.exception("notification dispatch failed")
            result.errors.append("dispatch")
        try:
            result.newly_overdue = self._check_overdue(clock.now())
        except Exception:
            logger.exception("overdue notification check failed")
            result.errors.append("overdue")
        if self._heartbeat is not None:
            try:
                self._heartbeat()
            except Exception:
                logger.exception("worker heartbeat failed")
                result.errors.append("heartbeat")
        return result

    def _check_overdue(self, now: datetime) -> int:
        """REQ-04 alert: notifications that passed deliver_by since the last check, undelivered."""
        since = self._overdue_checked_until or now - OVERDUE_LOOKBACK
        if now <= since:
            return 0
        with self._uow_factory() as uow:
            count = uow.notifications.count_undelivered_due_between(since, now)
        self._overdue_checked_until = now
        if count:
            logger.error(
                "REQ-04: %s notification(s) passed their maximum delay without reaching a "
                "webhook (is one registered and reachable?)",
                count,
            )
        return count


def record_heartbeat(session_factory: sessionmaker[Session], worker_id: str) -> None:
    now = SystemClock().now()
    stmt = insert(WorkerHeartbeat).values(id=1, worker_id=worker_id, beat_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"], set_={"worker_id": worker_id, "beat_at": now}
    )
    with session_factory.begin() as session:
        session.execute(stmt)


def read_heartbeat(session: Session) -> datetime | None:
    return session.scalar(select(WorkerHeartbeat.beat_at).where(WorkerHeartbeat.id == 1))


@contextmanager
def leadership(engine: Engine, stop: threading.Event, poll_s: float) -> Iterator[Connection | None]:
    """Hold the worker lock for the duration of the block (None if stopped while waiting)."""
    conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
    try:
        while not stop.is_set():
            got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": WORKER_LOCK_KEY})
            if got.scalar():
                yield conn
                return
            logger.info("another worker holds the lock; standing by")
            stop.wait(poll_s)
        yield None
    finally:
        conn.close()  # ends the session, which releases the lock


def main() -> int:
    import app.db.models  # noqa: F401  (every table, so flushes can order inserts by FK)
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.db.session import get_engine, get_sessionmaker
    from app.modules.testing.clock import install_simulated_clock

    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.test_routes_enabled:
        install_simulated_clock()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    session_factory = get_sessionmaker()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    worker = Worker(
        uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
        ledger=get_ledger(),
        notifier=get_notifier(),
        sender=sender_from_settings(),
        clock_provider=get_clock,
        max_attempts=settings.webhook_max_attempts,
        heartbeat=lambda: record_heartbeat(session_factory, worker_id),
    )
    with leadership(get_engine(), stop, settings.worker_poll_interval_s) as conn:
        if conn is None:
            return 0
        logger.info(
            "worker started (%s), polling every %ss", worker_id, settings.worker_poll_interval_s
        )
        while not stop.is_set():
            worker.run_once()
            try:
                conn.execute(text("SELECT 1"))
            except Exception:
                logger.critical("lost the database connection holding the worker lock; exiting")
                return 1
            stop.wait(settings.worker_poll_interval_s)
    logger.info("worker stopped")
    return 0
