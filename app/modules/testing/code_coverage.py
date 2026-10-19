"""Per-session code coverage for GET /test/report (course cross-team testing interface, D-19).

Line coverage of app/ in this API process, restarted at every POST /test/setup. It uses
coverage.py's sys.monitoring core, which traces every thread, including the server's request
threads that were already running when the session started (the default C tracer only sees
threads started afterwards).

The testing interface's own modules are left out, so reading a report doesn't change it. The
worker runs in another process and isn't measured. When measurement isn't possible, covered and
total are None and the basis says why; a 0 is never substituted.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parents[2]
_TESTING_DIR = Path(__file__).resolve().parent
# Code run by /test/setup and /test/report themselves.
EXCLUDED = frozenset(
    _TESTING_DIR / name for name in ("code_coverage.py", "sessions.py", "session_router.py")
)
BASIS = (
    "line coverage of app/ in the API process since POST /test/setup for this session "
    "(the worker process and the testing interface itself are not measured)"
)


@dataclass(frozen=True)
class CodeCoverage:
    covered: int | None
    total: int | None
    basis: str


def unavailable(reason: str) -> CodeCoverage:
    return CodeCoverage(covered=None, total=None, basis=f"not measured: {reason}")


class CoverageMeter:
    """One measurement at a time; restart() discards the previous one."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cov: Any = None
        self._reason = "no session has been set up"

    def restart(self) -> None:
        with self._lock:
            self._stop()
            try:
                import coverage
            except ImportError:
                self._reason = "coverage.py isn't installed in this image (INSTALL_COVERAGE=true)"
                return
            if coverage.Coverage.current() is not None:
                self._reason = (
                    "this process is already measured by an outer coverage run "
                    "(docker-compose.coverage.yml); use that report"
                )
                return
            cov = coverage.Coverage(
                data_file=None,
                config_file=False,
                source=[str(APP_DIR)],
                omit=[str(p) for p in EXCLUDED],
            )
            cov.set_option("run:core", "sysmon")
            try:
                cov.start()
            except Exception as exc:
                self._reason = f"coverage.py could not start ({exc})"
                return
            self._cov = cov

    def snapshot(self) -> CodeCoverage:
        with self._lock:
            if self._cov is None:
                return unavailable(self._reason)
            covered = total = 0
            for path in sorted(APP_DIR.rglob("*.py")):
                if path in EXCLUDED:
                    continue
                _, statements, _, missing, _ = self._cov.analysis2(str(path))
                total += len(statements)
                covered += len(statements) - len(missing)
            return CodeCoverage(covered=covered, total=total, basis=BASIS)

    def stop(self) -> None:
        with self._lock:
            self._stop()
            self._reason = "measurement stopped"

    def _stop(self) -> None:
        if self._cov is not None:
            self._cov.stop()
            self._cov = None


_meter: CoverageMeter | None = None


def get_coverage_meter() -> CoverageMeter:
    """FastAPI dependency: this process's meter (replaced by a double in tests)."""
    global _meter
    if _meter is None:
        _meter = CoverageMeter()
    return _meter
