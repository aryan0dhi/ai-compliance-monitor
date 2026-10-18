"""GET /v1/events and GET /v1/events/{id} business logic (events.service)."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.errors import UnimplementedError
from app.modules.auth.interface import Role
from app.modules.events.models import Event, EventState, EventType, retain_until_for
from app.modules.events.repository import EventFilters
from app.modules.events.service import EventAccessDenied, EventNotFound, get_event, list_events
from app.modules.ledger.interface import LedgerEntryType
from tests.conftest import T0
from tests.fakes import system_principal, user_principal

VIEWER = user_principal(Role.VIEWER)


def make_event(system_id, *, minutes=0, type_="threshold_breach", state="FLAGGED"):
    detected = T0 + timedelta(minutes=minutes)
    breach = type_ == "threshold_breach"
    return Event(
        id=uuid4(),
        type=type_,
        state=state,
        system_id=system_id,
        metric_name="drift_score" if breach else None,
        metric_timestamp=detected if breach else None,
        observed_value=Decimal("0.35") if breach else None,
        threshold_version_id=uuid4() if breach else None,
        threshold_value=Decimal("0.30") if breach else None,
        detected_at=detected,
        retain_until=retain_until_for(detected),
    )


@pytest.fixture
def seeded(uow):
    a, b = uuid4(), uuid4()
    events = [
        make_event(a, minutes=0),
        make_event(a, minutes=10, state="UNDER_REVIEW"),
        make_event(b, minutes=20, type_="monitoring_gap", state="GAP_OPEN"),
        make_event(b, minutes=30),
    ]
    for e in events:
        uow.events.rows[e.id] = e
    return a, b, events


class TestListEvents:
    def test_newest_first(self, uow, seeded):
        _, _, events = seeded
        page = list_events(EventFilters(), limit=50, offset=0, principal=VIEWER, uow=uow)
        assert [e.id for e in page.items] == [e.id for e in reversed(events)]
        assert page.total == 4

    def test_filters(self, uow, seeded):
        a, b, events = seeded

        def ids(**kw):
            page = list_events(EventFilters(**kw), limit=50, offset=0, principal=VIEWER, uow=uow)
            return {e.id for e in page.items}

        assert ids(system_id=a) == {events[0].id, events[1].id}
        assert ids(state=EventState.UNDER_REVIEW) == {events[1].id}
        assert ids(type=EventType.MONITORING_GAP) == {events[2].id}
        assert ids(since=T0 + timedelta(minutes=20)) == {events[2].id, events[3].id}
        assert ids(system_id=b, type=EventType.THRESHOLD_BREACH) == {events[3].id}

    def test_pagination(self, uow, seeded):
        _, _, events = seeded
        page = list_events(EventFilters(), limit=2, offset=1, principal=VIEWER, uow=uow)
        assert [e.id for e in page.items] == [events[2].id, events[1].id]
        assert (page.total, page.limit, page.offset) == (4, 2, 1)

    def test_system_keys_cannot_list(self, uow, seeded):
        with pytest.raises(EventAccessDenied):
            list_events(
                EventFilters(), limit=50, offset=0, principal=system_principal(uuid4()), uow=uow
            )


class TestGetEvent:
    @pytest.mark.parametrize("role", [Role.VIEWER, Role.REVIEWER, Role.ADMIN])
    def test_returns_event_and_logs_view(self, uow, ledger, seeded, role):
        _, _, events = seeded
        principal = user_principal(role)
        event = get_event(events[0].id, principal=principal, uow=uow, ledger=ledger)
        assert event is events[0]
        assert ledger.calls == [
            {
                "session": None,
                "entry_type": LedgerEntryType.VIEW,
                "actor_id": principal.actor_id,
                "payload": {"event_id": str(events[0].id)},
                "event_id": events[0].id,
            }
        ]
        assert uow.commits == 1

    def test_unknown_event_writes_no_ledger_entry(self, uow, ledger, seeded):
        with pytest.raises(EventNotFound):
            get_event(uuid4(), principal=VIEWER, uow=uow, ledger=ledger)
        assert ledger.calls == []

    def test_view_fails_when_ledger_unavailable(self, uow, seeded):
        """REQ-14: no unaudited views. If the VIEW entry can't be written, the read fails."""
        from tests.fakes import RecordingLedger

        _, _, events = seeded
        failing = RecordingLedger(fail_with=UnimplementedError("x", "REQ-14", "ledger"))
        with pytest.raises(UnimplementedError):
            get_event(events[0].id, principal=VIEWER, uow=uow, ledger=failing)
        assert uow.rollbacks == 1

    def test_system_keys_cannot_read(self, uow, ledger, seeded):
        _, _, events = seeded
        with pytest.raises(EventAccessDenied):
            get_event(events[0].id, principal=system_principal(uuid4()), uow=uow, ledger=ledger)
        assert ledger.calls == []
