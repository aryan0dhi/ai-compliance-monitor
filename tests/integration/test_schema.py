"""Migrations and database constraints against real PostgreSQL (REQ-01, REQ-03, REQ-05)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Base
from app.modules.events.models import retain_until_for
from tests.conftest import T0
from tests.integration.conftest import migrate, throwaway_database

# --- migrations ---------------------------------------------------------------------------------


def test_upgrade_downgrade_upgrade_round_trip(postgres_server_url):
    with throwaway_database(postgres_server_url) as engine:
        migrate(engine)
        assert {"monitored_system", "metric_sample", "threshold_version", "event"} <= set(
            inspect(engine).get_table_names()
        )
        migrate(engine, "base", downgrade=True)
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
        migrate(engine)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002"


def test_models_and_migrations_have_no_drift(pg_engine):
    """The online equivalent of tests/test_migrations.py: autogenerate finds nothing to do."""
    with pg_engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


# --- constraints ---------------------------------------------------------------------------------

SYS = uuid.UUID("550e8400-e29b-41d4-a716-446655440000")
TV = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


@pytest.fixture
def conn(pg_sessionmaker, pg_engine):
    """A connection with one system, one threshold and one sample; rolled back after the test."""
    with pg_engine.connect() as c:
        tx = c.begin()
        c.execute(
            text("INSERT INTO monitored_system (id, name) VALUES (:id, 'loan-scorer')"), {"id": SYS}
        )
        c.execute(
            text(
                "INSERT INTO threshold_version (id, system_id, metric_name, metric_type, direction,"
                " limit_value, valid_from) VALUES (:id, :s, 'drift_score', 'drift', 'above', 0.30,"
                " :vf)"
            ),
            {"id": TV, "s": SYS, "vf": T0 - timedelta(days=1)},
        )
        c.execute(
            text(
                "INSERT INTO metric_sample (system_id, metric_name, sampled_at, metric_type, value,"
                " received_at) VALUES (:s, 'drift_score', :t, 'drift', 0.35, :t)"
            ),
            {"s": SYS, "t": T0},
        )
        yield c
        tx.rollback()


def violates(conn, sql: str, params: dict | None = None) -> bool:
    """True if the statement is rejected with an IntegrityError (checked in a savepoint).

    On success the savepoint is released, so the row stays visible to later statements in the
    test (e.g. a second insert that must collide with it). The `conn` fixture's outer
    transaction still rolls everything back at the end of the test.
    """
    sp = conn.begin_nested()
    try:
        conn.execute(text(sql), params or {})
    except IntegrityError:
        sp.rollback()
        return True
    sp.commit()
    return False


def insert_event(conn, **overrides) -> bool:
    detected = overrides.pop("detected_at", T0)
    values = {
        "id": uuid.uuid4(),
        "type": "threshold_breach",
        "state": "FLAGGED",
        "system_id": SYS,
        "metric_name": "drift_score",
        "metric_timestamp": T0,
        "observed_value": Decimal("0.35"),
        "threshold_version_id": TV,
        "threshold_value": Decimal("0.30"),
        "detected_at": detected,
        "retain_until": retain_until_for(detected),
        "evidence_bundle_hash": None,
    }
    values.update(overrides)
    cols = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    return not violates(conn, f"INSERT INTO event ({cols}) VALUES ({params})", values)


def test_valid_breach_event_accepted(conn):
    assert insert_event(conn)


@pytest.mark.parametrize(
    "missing",
    [
        "metric_name",
        "metric_timestamp",
        "observed_value",
        "threshold_version_id",
        "threshold_value",
    ],
)
def test_breach_event_requires_req03_fields(conn, missing):
    assert not insert_event(conn, **{missing: None})


def test_gap_events_may_omit_breach_fields_and_do_not_collide(conn):
    gap = dict(
        type="monitoring_gap",
        metric_name=None,
        metric_timestamp=None,
        observed_value=None,
        threshold_version_id=None,
        threshold_value=None,
    )
    assert insert_event(conn, **gap)
    assert insert_event(conn, **gap)  # NULLs never conflict in uq_event_triggering_sample


def test_one_event_per_triggering_sample(conn):
    assert insert_event(conn)
    assert not insert_event(conn)


def test_event_requires_existing_triggering_sample(conn):
    assert not insert_event(conn, metric_timestamp=T0 + timedelta(minutes=5))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("type", "BREACH"),
        ("state", "CLASSIFIED"),
        ("evidence_bundle_hash", "ABC"),
        ("evidence_bundle_hash", "g" * 64),
    ],
)
def test_event_check_constraints(conn, column, value):
    assert not insert_event(conn, **{column: value})


def test_valid_evidence_hash_accepted(conn):
    assert insert_event(conn, evidence_bundle_hash="a" * 64)


class TestRetentionConstraint:
    """REQ-05 storage / INV-10: retain_until >= detected_at + 6 months, computed in UTC."""

    def test_less_than_six_months_rejected(self, conn):
        assert not insert_event(conn, retain_until=retain_until_for(T0) - timedelta(seconds=1))

    def test_exactly_six_months_accepted(self, conn):
        assert insert_event(conn, retain_until=retain_until_for(T0))

    @pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Pacific/Kiritimati"])
    def test_month_end_boundary_independent_of_session_time_zone(self, conn, tz):
        conn.execute(text(f"SET LOCAL TIME ZONE '{tz}'"))
        detected = datetime(2026, 8, 31, 23, 30, tzinfo=UTC)
        # Needs a matching sample for the triggering-sample FK.
        conn.execute(
            text(
                "INSERT INTO metric_sample (system_id, metric_name, sampled_at, metric_type,"
                " value, received_at) VALUES (:s, 'drift_score', :t, 'drift', 0.35, :t)"
                " ON CONFLICT DO NOTHING"
            ),
            {"s": SYS, "t": detected},
        )
        assert insert_event(conn, detected_at=detected, metric_timestamp=detected)


@pytest.mark.parametrize(
    "detected",
    [
        datetime(2026, 9, 27, 20, 1, tzinfo=UTC),
        datetime(2026, 8, 31, 23, 30, tzinfo=UTC),
        datetime(2027, 8, 31, 0, 0, tzinfo=UTC),  # -> leap day 2028-02-29
        datetime(2026, 12, 31, 12, 0, tzinfo=UTC),
        datetime(2026, 3, 31, 1, 0, tzinfo=UTC),
    ],
)
def test_python_retain_until_matches_postgres(pg_engine, detected):
    with pg_engine.connect() as c:
        pg = c.execute(
            text("SELECT ((:d AT TIME ZONE 'UTC') + interval '6 months') AT TIME ZONE 'UTC'"),
            {"d": detected},
        ).scalar()
    assert pg == retain_until_for(detected)


class TestOtherConstraints:
    def test_nan_metric_value_rejected(self, conn):
        assert violates(
            conn,
            "INSERT INTO metric_sample (system_id, metric_name, sampled_at, metric_type, value,"
            " received_at) VALUES (:s, 'm', :t, 'drift', 'NaN', :t)",
            {"s": SYS, "t": T0},
        )

    def test_bad_metric_type_rejected(self, conn):
        assert violates(
            conn,
            "INSERT INTO metric_sample (system_id, metric_name, sampled_at, metric_type, value,"
            " received_at) VALUES (:s, 'm', :t, 'latency', 1, :t)",
            {"s": SYS, "t": T0},
        )

    def test_duplicate_sample_key_rejected(self, conn):
        assert violates(
            conn,
            "INSERT INTO metric_sample (system_id, metric_name, sampled_at, metric_type, value,"
            " received_at) VALUES (:s, 'drift_score', :t, 'drift', 0.36, :t)",
            {"s": SYS, "t": T0},
        )

    @pytest.mark.parametrize(("direction", "limit"), [("sideways", "0.5"), ("above", "NaN")])
    def test_threshold_checks(self, conn, direction, limit):
        assert violates(
            conn,
            "INSERT INTO threshold_version (id, system_id, metric_name, metric_type, direction,"
            " limit_value, valid_from)"
            " VALUES (:id, :s, 'x', 'drift', :d, CAST(:l AS numeric), :vf)",
            {"id": uuid.uuid4(), "s": SYS, "d": direction, "l": limit, "vf": T0},
        )

    def test_duplicate_threshold_valid_from_rejected(self, conn):
        assert violates(
            conn,
            "INSERT INTO threshold_version (id, system_id, metric_name, metric_type, direction,"
            " limit_value, valid_from) VALUES (:id, :s, 'drift_score', 'drift', 'above', 0.5, :vf)",
            {"id": uuid.uuid4(), "s": SYS, "vf": T0 - timedelta(days=1)},
        )

    def test_sampling_interval_must_be_positive(self, conn):
        assert violates(
            conn,
            "INSERT INTO monitored_system (id, name, sampling_interval_s) VALUES (:id, 'x', 0)",
            {"id": uuid.uuid4()},
        )


class TestRestrictForeignKeys:
    """Nothing an event depends on can be deleted out from under it (REQ-05)."""

    @pytest.fixture(autouse=True)
    def _event(self, conn):
        assert insert_event(conn)

    def test_triggering_sample(self, conn):
        assert violates(conn, "DELETE FROM metric_sample WHERE system_id = :s", {"s": SYS})

    def test_threshold_version(self, conn):
        assert violates(conn, "DELETE FROM threshold_version WHERE id = :id", {"id": TV})

    def test_monitored_system(self, conn):
        assert violates(conn, "DELETE FROM monitored_system WHERE id = :id", {"id": SYS})


def test_timestamps_round_trip_as_utc(conn):
    value = conn.execute(
        text("SELECT sampled_at FROM metric_sample WHERE system_id = :s"), {"s": SYS}
    ).scalar()
    assert value == T0 and value.utcoffset() == timedelta(0)
