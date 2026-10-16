from datetime import UTC, datetime

import pytest

from tests.fakes import FakeUnitOfWork, FixedClock, RecordingLedger, RecordingNotifier

T0 = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def ledger() -> RecordingLedger:
    return RecordingLedger()


@pytest.fixture
def notifier() -> RecordingNotifier:
    return RecordingNotifier()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0.replace(minute=1))
