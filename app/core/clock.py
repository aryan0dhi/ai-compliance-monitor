"""Clock interface.

All code that needs "now" (ingestion timestamps, gap detection, deadlines, retention) must read
time through this module so the pod's POST /v1/test/clock/advance can drive it in the test
environment (Phase 2, Section 5 and Q7.2 Decision 2). The simulated clock itself is owned by the
testing module and is not implemented yet.
"""

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    """FastAPI dependency / accessor for the active clock."""
    return _clock


def set_clock(clock: Clock) -> None:
    """Swap the active clock (test environment and unit tests only)."""
    global _clock
    _clock = clock
