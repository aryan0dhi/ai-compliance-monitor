"""Test sessions for POST /test/setup and GET /test/report (cross-team testing interface, D-19).

Setup resets the one shared test database and code coverage is measured per process, so only
one session is active at a time. A new setup ends the active session and freezes its report, so
an interrupted run never blocks the next one. Sessions live in this API process's memory: a
restart forgets them (their reports become 404) and the tester sets up again.
"""

import dataclasses
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.modules.testing.code_coverage import CodeCoverage, CoverageMeter

MAX_SESSIONS = 100  # oldest ended sessions are forgotten beyond this


class SessionState(StrEnum):
    SETTING_UP = "setting_up"
    ACTIVE = "active"
    ENDED = "ended"


@dataclass
class TestSession:
    __test__ = False  # not a pytest test class

    session_id: str
    setup: dict[str, Any]
    state: SessionState = SessionState.SETTING_UP
    final_coverage: CodeCoverage | None = field(default=None)


class SessionExists(Exception):
    pass


class SetupInProgress(Exception):
    pass


class SessionNotReady(Exception):
    pass


class UnknownSession(Exception):
    pass


class TestSessions:
    __test__ = False

    def __init__(self, meter: CoverageMeter, *, max_sessions: int = MAX_SESSIONS) -> None:
        self._meter = meter
        self._max = max_sessions
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, TestSession] = OrderedDict()

    def begin(self, session_id: str, setup: dict[str, Any]) -> None:
        """Register a session in SETTING_UP and end the active one (its report is frozen)."""
        with self._lock:
            if session_id in self._sessions:
                raise SessionExists(session_id)
            if any(s.state is SessionState.SETTING_UP for s in self._sessions.values()):
                raise SetupInProgress("another session is being set up")
            for s in self._sessions.values():
                if s.state is SessionState.ACTIVE:
                    final = self._meter.snapshot()
                    s.state = SessionState.ENDED
                    s.final_coverage = dataclasses.replace(
                        final, basis=f"{final.basis}; frozen when session {session_id!r} began"
                    )
            self._sessions[session_id] = TestSession(session_id=session_id, setup=setup)
            while len(self._sessions) > self._max:
                self._sessions.popitem(last=False)

    def activate(self, session_id: str) -> None:
        """Setup finished: start measuring this session from zero."""
        with self._lock:
            self._meter.restart()
            self._sessions[session_id].state = SessionState.ACTIVE

    def abandon(self, session_id: str) -> None:
        """Setup failed: forget the session, so the same id can be set up again."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def coverage(self, session_id: str) -> CodeCoverage:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise UnknownSession(session_id)
            if session.state is SessionState.SETTING_UP:
                raise SessionNotReady(session_id)
            if session.final_coverage is not None:
                return session.final_coverage
            return self._meter.snapshot()


_sessions: TestSessions | None = None


def get_test_sessions() -> TestSessions:
    """FastAPI dependency: this process's sessions (replaced in tests)."""
    global _sessions
    if _sessions is None:
        from app.modules.testing.code_coverage import get_coverage_meter

        _sessions = TestSessions(get_coverage_meter())
    return _sessions
