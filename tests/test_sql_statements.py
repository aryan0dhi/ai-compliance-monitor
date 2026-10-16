"""SQL repository statements, compiled with the PostgreSQL dialect (no database needed).

These catch query-construction mistakes only. Executing the SQL repositories against PostgreSQL
is part of the Docker integration backlog.
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.modules.events.models import EventState, EventType
from app.modules.events.repository import EventFilters, build_count_query, build_list_query
from app.modules.metrics.models import MetricSample
from app.modules.metrics.repository import build_insert_if_absent
from app.modules.thresholds.repository import build_in_force_query

T = datetime(2026, 9, 27, 20, 0, tzinfo=UTC)


def sql(stmt) -> str:
    return " ".join(str(stmt.compile(dialect=postgresql.dialect())).split())


def test_insert_if_absent_is_race_safe_upsert():
    sample = MetricSample(
        system_id=uuid4(),
        metric_name="drift_score",
        sampled_at=T,
        metric_type="drift",
        value=Decimal("0.35"),
        received_at=T,
    )
    s = sql(build_insert_if_absent(sample))
    assert s.startswith("INSERT INTO metric_sample")
    assert "ON CONFLICT (system_id, metric_name, sampled_at) DO NOTHING" in s
    assert "RETURNING metric_sample.system_id" in s


def test_threshold_in_force_query():
    s = sql(build_in_force_query(uuid4(), "drift_score", T))
    assert "threshold_version.valid_from <= " in s
    assert "ORDER BY threshold_version.valid_from DESC" in s
    assert "LIMIT" in s


def test_event_list_without_filters():
    s = sql(build_list_query(EventFilters(), limit=50, offset=0))
    assert "WHERE" not in s
    assert "ORDER BY event.detected_at DESC, event.id DESC" in s
    assert "LIMIT" in s and "OFFSET" in s


def test_event_list_with_all_filters():
    filters = EventFilters(
        system_id=uuid4(), state=EventState.FLAGGED, type=EventType.THRESHOLD_BREACH, since=T
    )
    s = sql(build_list_query(filters, limit=10, offset=20))
    for clause in [
        "event.system_id = ",
        "event.state = ",
        "event.type = ",
        "event.detected_at >= ",
    ]:
        assert clause in s
    params = (
        build_list_query(filters, limit=10, offset=20).compile(dialect=postgresql.dialect()).params
    )
    assert "FLAGGED" in params.values() and "threshold_breach" in params.values()


def test_event_count_uses_same_filters():
    filters = EventFilters(state=EventState.UNDER_REVIEW)
    s = sql(build_count_query(filters))
    assert s.startswith("SELECT count(*) AS count_1 FROM event WHERE event.state = ")
    assert "LIMIT" not in s
