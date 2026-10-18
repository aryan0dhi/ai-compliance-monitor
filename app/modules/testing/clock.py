"""Simulated clock for the test environment (POST /v1/test/clock/advance, Section 5.1).

Simulated time is real UTC time plus an offset that only grows. The offset lives in the database
(simulated_clock) because the API and the worker are separate processes and must agree on "now";
each process caches it for up to a second. Installed only when test routes are enabled, which is
never the case with APP_ENV=production.
"""

import threading
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import set_clock
from app.modules.testing.models import SimulatedClockState

CACHE_TTL_S = 1.0


class SimulatedClock:
    def __init__(self, session_factory: sessionmaker[Session], *, cache_ttl_s: float = CACHE_TTL_S):
        self._session_factory = session_factory
        self._cache_ttl_s = cache_ttl_s
        self._lock = threading.Lock()
        self._offset = 0
        self._loaded_at: float | None = None

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset_seconds())

    def offset_seconds(self) -> int:
        with self._lock:
            stale = self._loaded_at is None or (
                time.monotonic() - self._loaded_at >= self._cache_ttl_s
            )
            if stale:
                with self._session_factory() as session:
                    stored = session.scalar(
                        select(SimulatedClockState.offset_seconds).where(
                            SimulatedClockState.id == 1
                        )
                    )
                self._offset = stored or 0
                self._loaded_at = time.monotonic()
            return self._offset

    def advance(self, seconds: int) -> datetime:
        if seconds < 0:
            raise ValueError("the simulated clock only moves forward")
        row = insert(SimulatedClockState).values(id=1, offset_seconds=seconds)
        upsert = row.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "offset_seconds": SimulatedClockState.offset_seconds + row.excluded.offset_seconds
            },
        ).returning(SimulatedClockState.offset_seconds)
        with self._session_factory.begin() as session:
            new_offset = session.execute(upsert).scalar_one()
        with self._lock:
            self._offset, self._loaded_at = new_offset, time.monotonic()
        return self.now()

    def invalidate(self) -> None:
        """Forget the cached offset (after /v1/test/reset changed it)."""
        with self._lock:
            self._loaded_at = None


_simulated: SimulatedClock | None = None


def get_simulated_clock() -> SimulatedClock:
    """FastAPI dependency: this process's simulated clock (replaced by a double in tests)."""
    global _simulated
    if _simulated is None:
        from app.db.session import get_sessionmaker

        _simulated = SimulatedClock(get_sessionmaker())
    return _simulated


def install_simulated_clock() -> SimulatedClock:
    """Make the simulated clock the one returned by app.core.clock.get_clock()."""
    clock = get_simulated_clock()
    set_clock(clock)
    return clock
