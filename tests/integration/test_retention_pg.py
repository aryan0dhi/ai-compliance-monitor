"""REQ-05 / INV-10 on PostgreSQL: no event is deleted, nor its retain_until shortened, before its
retention ends, even by direct SQL (#69, migration 0013). Time is the service clock."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError

from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.testing.models import SimulatedClockState


@pytest.fixture
def gap_event(pg_sessionmaker, seed):
    """A monitoring-gap event (no triggering sample) detected at `detected_at`."""

    def _make(detected_at: datetime) -> uuid.UUID:
        system_id, _, _ = seed()
        event = Event(
            id=uuid.uuid4(),
            type=EventType.MONITORING_GAP,
            state=EventState.GAP_OPEN,
            system_id=system_id,
            detected_at=detected_at,
            retain_until=retain_until_for(detected_at),
            gap_started_at=detected_at - timedelta(minutes=15),
        )
        with pg_sessionmaker.begin() as s:
            s.add(event)
        return event.id

    return _make


def _delete(pg_sessionmaker, event_id):
    with pg_sessionmaker.begin() as s:
        s.execute(delete(Event).where(Event.id == event_id))


def _exists(pg_sessionmaker, event_id) -> bool:
    with pg_sessionmaker() as s:
        return s.get(Event, event_id) is not None


def test_an_event_within_retention_cannot_be_deleted(pg_sessionmaker, gap_event):
    event_id = gap_event(datetime.now(UTC))
    with pytest.raises(DBAPIError, match="must be retained until"):
        _delete(pg_sessionmaker, event_id)
    assert _exists(pg_sessionmaker, event_id)


def test_an_event_past_retention_can_be_deleted(pg_sessionmaker, gap_event):
    event_id = gap_event(datetime(2020, 1, 1, tzinfo=UTC))
    _delete(pg_sessionmaker, event_id)
    assert not _exists(pg_sessionmaker, event_id)


def test_the_simulated_clock_counts(pg_sessionmaker, gap_event):
    """In the test environment, advancing the clock past retain_until allows the deletion."""
    event_id = gap_event(datetime.now(UTC))
    with pg_sessionmaker.begin() as s:
        s.add(SimulatedClockState(id=1, offset_seconds=int(timedelta(days=200).total_seconds())))
    _delete(pg_sessionmaker, event_id)
    assert not _exists(pg_sessionmaker, event_id)


def test_retain_until_can_move_later_but_never_earlier(pg_sessionmaker, gap_event):
    detected = datetime.now(UTC)
    event_id = gap_event(detected)
    later = retain_until_for(detected) + timedelta(days=30)
    with pg_sessionmaker.begin() as s:
        s.execute(update(Event).where(Event.id == event_id).values(retain_until=later))
    with pytest.raises(DBAPIError, match="can only move later"):
        with pg_sessionmaker.begin() as s:
            s.execute(
                update(Event)
                .where(Event.id == event_id)
                .values(retain_until=later - timedelta(days=1))
            )
    with pg_sessionmaker() as s:
        assert s.get(Event, event_id).retain_until == later


def test_other_updates_are_unaffected(pg_sessionmaker, gap_event):
    event_id = gap_event(datetime.now(UTC))
    with pg_sessionmaker.begin() as s:
        s.execute(
            update(Event)
            .where(Event.id == event_id)
            .values(state=EventState.GAP_CLOSED, gap_ended_at=datetime.now(UTC))
        )
    with pg_sessionmaker() as s:
        assert s.get(Event, event_id).state == EventState.GAP_CLOSED


def test_truncate_for_the_test_reset_still_works(pg_sessionmaker, gap_event):
    gap_event(datetime.now(UTC))
    with pg_sessionmaker.begin() as s:
        s.execute(text("TRUNCATE event CASCADE"))
    with pg_sessionmaker() as s:
        assert s.execute(text("SELECT count(*) FROM event")).scalar_one() == 0
