"""AC-01 timestamp-log check (REQ-01): a sample every sampling interval, within a tolerance.

Pure function over arrival times, shared by the unit, PostgreSQL and contract tests (it imports
nothing from the application, so the contract suite stays black box). A sample more than the
tolerance off schedule is a cadence violation reported by this check; the service itself doesn't
flag it, and it opens no gap unless data stops for 3 intervals (docs/spec-decisions.md D-17).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

AC01_INTERVAL_S = 300
AC01_TOLERANCE_S = 10


@dataclass(frozen=True)
class CadenceViolation:
    index: int  # position in the sorted log of the sample that arrived off schedule
    interval: timedelta  # time since the previous sample


def cadence_violations(
    times: Iterable[datetime],
    *,
    interval_s: float = AC01_INTERVAL_S,
    tolerance_s: float = AC01_TOLERANCE_S,
) -> list[CadenceViolation]:
    """Intervals between consecutive samples that are not interval_s ± tolerance_s (inclusive)."""
    log = sorted(times)
    expected, tolerance = timedelta(seconds=interval_s), timedelta(seconds=tolerance_s)
    return [
        CadenceViolation(index=i, interval=log[i] - log[i - 1])
        for i in range(1, len(log))
        if abs(log[i] - log[i - 1] - expected) > tolerance
    ]
