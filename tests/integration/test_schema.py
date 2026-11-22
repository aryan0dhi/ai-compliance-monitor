"""Migrations and database constraints against real PostgreSQL (REQ-01, REQ-03, REQ-05)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Base
from app.modules.events.models import retain_until_for
from tests.conftest import T0
from tests.integration.conftest import alembic_config, migrate, throwaway_database

# --- migrations ---------------------------------------------------------------------------------


def test_upgrade_downgrade_upgrade_round_trip(postgres_server_url):
    with throwaway_database(postgres_server_url) as engine:
        migrate(engine)
        assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
        migrate(engine, "base", downgrade=True)
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
        migrate(engine)
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == ScriptDirectory.from_config(alembic_config(None)).get_current_head()


def test_0007_backfills_deadlines_by_replaying_dispositions(postgres_server_url):
    """Rows written under 0006 get the B-10 deadline in force after each of them."""
    with throwaway_database(postgres_server_url) as engine:
        migrate(engine, "0006")
        event, user = uuid.uuid4(), uuid.uuid4()
        a = T0
        with engine.begin() as c:
            c.execute(
                text("INSERT INTO monitored_system (id, name) VALUES (:id, 's')"), {"id": SYS}
            )
            c.execute(
                text(
                    "INSERT INTO event (id, type, state, system_id, detected_at, retain_until,"
                    " gap_started_at) VALUES (:id, 'monitoring_gap', 'GAP_OPEN', :s, :t, :r, :t)"
                ),
                {"id": event, "s": SYS, "t": T0, "r": retain_until_for(T0)},
            )
            c.execute(
                text("INSERT INTO app_user (id, name, role) VALUES (:id, 'r', 'reviewer')"),
                {"id": user},
            )
            rows = [  # (tier, previous, rationale, at)
                ("NOT_SERIOUS", None, "first look", a - timedelta(days=1)),
                ("OTHER_SERIOUS", "NOT_SERIOUS", None, a),  # A
                ("DEATH", "OTHER_SERIOUS", None, a + timedelta(days=3)),  # stricter: A + 10
                ("OTHER_SERIOUS", "DEATH", None, a + timedelta(days=4)),  # looser: A + 15
                ("OTHER_SERIOUS", "OTHER_SERIOUS", None, a + timedelta(days=5)),  # same: kept
            ]
            for seq, (tier, prev, why, at) in enumerate(rows, start=1):
                c.execute(
                    text(
                        "INSERT INTO disposition (event_id, seq, tier, previous_tier, reviewer_id,"
                        " rationale, recorded_at) VALUES (:e, :n, :t, :p, :u, :w, :at)"
                    ),
                    {"e": event, "n": seq, "t": tier, "p": prev, "u": user, "w": why, "at": at},
                )
        migrate(engine, "0007")
        with engine.connect() as c:
            got = c.execute(text("SELECT deadline_utc FROM disposition ORDER BY seq")).scalars()
            assert list(got) == [
                None,
                a + timedelta(days=15),
                a + timedelta(days=10),
                a + timedelta(days=15),
                a + timedelta(days=15),
            ]


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


GAP = dict(
    type="monitoring_gap",
    state="GAP_CLOSED",
    metric_name=None,
    metric_timestamp=None,
    observed_value=None,
    threshold_version_id=None,
    threshold_value=None,
    gap_started_at=T0 - timedelta(minutes=15),
    gap_ended_at=T0 + timedelta(hours=1),
)


def test_gap_events_may_omit_breach_fields_and_do_not_collide(conn):
    assert insert_event(conn, **GAP)
    assert insert_event(conn, **GAP)  # NULLs never conflict in uq_event_triggering_sample


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


class TestSystemHarmFields:
    """D-20 (migration 0004): a system lists only known Art. 3(49) fields; default none."""

    def test_default_is_empty(self, conn):
        fields = conn.execute(
            text("SELECT harm_fields FROM monitored_system WHERE id = :id"), {"id": SYS}
        ).scalar()
        assert fields == []

    def test_known_fields_accepted(self, conn):
        assert not violates(
            conn,
            "UPDATE monitored_system SET harm_fields = "
            "ARRAY['fundamental_rights', 'health_safety'] WHERE id = :id",
            {"id": SYS},
        )

    def test_unknown_field_rejected(self, conn):
        assert violates(
            conn,
            "UPDATE monitored_system SET harm_fields = ARRAY['reputation'] WHERE id = :id",
            {"id": SYS},
        )


class TestHarmFieldConstraints:
    """Migration 0005 (B-07): one row per event and field; confirmation needs a time."""

    @pytest.fixture
    def event_id(self, conn):
        event_id = uuid.uuid4()
        assert insert_event(conn, id=event_id)
        return event_id

    def harm(self, conn, event_id=None, **values) -> bool:
        row = {"event_id": event_id, "name": "fundamental_rights", **values}
        cols = ", ".join(row)
        params = ", ".join(f":{k}" for k in row)
        return not violates(conn, f"INSERT INTO harm_field ({cols}) VALUES ({params})", row)

    def test_pending_by_default(self, conn, event_id):
        assert self.harm(conn, event_id)
        status = conn.execute(
            text("SELECT status FROM harm_field WHERE event_id = :e"), {"e": event_id}
        ).scalar()
        assert status == "PENDING"

    def test_one_row_per_event_and_field(self, conn, event_id):
        assert self.harm(conn, event_id)
        assert not self.harm(conn, event_id)

    @pytest.mark.parametrize(
        "values",
        [
            {"name": "reputation"},
            {"status": "MAYBE"},
            {"status": "CONFIRMED"},  # no confirmed_at
            {"status": "PENDING", "confirmed_at": T0},
            {"event_id": uuid.uuid4()},  # no such event
        ],
    )
    def test_invalid_rows_rejected(self, conn, event_id, values):
        assert not self.harm(conn, **{"event_id": event_id, **values})

    def test_confirmed_row_accepted(self, conn, event_id):
        assert self.harm(conn, event_id, status="NOT_APPLICABLE", confirmed_at=T0)

    def test_event_with_harm_fields_cannot_be_deleted(self, conn, event_id):
        assert self.harm(conn, event_id)
        assert violates(conn, "DELETE FROM event WHERE id = :e", {"e": event_id})


class TestDispositionConstraints:
    """Migration 0006 (B-08, B-09): insert-only tier history, numbered per event."""

    @pytest.fixture
    def event_id(self, conn):
        event_id = uuid.uuid4()
        assert insert_event(conn, id=event_id)
        return event_id

    @pytest.fixture
    def reviewer(self, conn):
        return insert_user(conn, role="reviewer")

    def dispose(self, conn, event, reviewer, **values) -> bool:
        row = {
            "event_id": event,
            "seq": 1,
            "tier": "OTHER_SERIOUS",
            "previous_tier": None,
            "reviewer_id": reviewer,
            "rationale": None,
            "recorded_at": T0,
            "deadline_utc": T0 + timedelta(days=15),
            **values,
        }
        cols = ", ".join(row)
        params = ", ".join(f":{k}" for k in row)
        return not violates(conn, f"INSERT INTO disposition ({cols}) VALUES ({params})", row)

    def test_first_and_revision_accepted(self, conn, event_id, reviewer):
        assert self.dispose(conn, event_id, reviewer)
        assert self.dispose(
            conn, event_id, reviewer, seq=2, tier="DEATH", previous_tier="OTHER_SERIOUS"
        )

    def test_one_row_per_event_and_seq(self, conn, event_id, reviewer):
        assert self.dispose(conn, event_id, reviewer)
        assert not self.dispose(conn, event_id, reviewer, tier="DEATH")

    @pytest.mark.parametrize(
        "values",
        [
            {"tier": "CRIT_INFRA"},
            {"seq": 0},
            {"previous_tier": "DEATH"},  # the first replaces nothing
            {"seq": 2},  # a revision must name what it replaced
            {"seq": 2, "previous_tier": "SEVERE"},
            {"tier": "NOT_SERIOUS"},
            {"tier": "NOT_SERIOUS", "rationale": "  "},
            {"reviewer_id": uuid.uuid4()},  # no such user
            {"event_id": uuid.uuid4()},  # no such event
            {"recorded_at": None},
            {"deadline_utc": None},  # a serious tier always has a deadline (B-10)
            {"tier": "NOT_SERIOUS", "rationale": "no harm"},  # ... and NOT_SERIOUS never does
        ],
    )
    def test_invalid_rows_rejected(self, conn, event_id, reviewer, values):
        assert not self.dispose(conn, event_id, reviewer, **values)

    def test_not_serious_with_rationale_accepted(self, conn, event_id, reviewer):
        assert self.dispose(
            conn, event_id, reviewer, tier="NOT_SERIOUS", rationale="no harm", deadline_utc=None
        )

    def test_dispositioned_event_cannot_be_deleted(self, conn, event_id, reviewer):
        assert self.dispose(conn, event_id, reviewer)
        assert violates(conn, "DELETE FROM event WHERE id = :e", {"e": event_id})


class TestReportConstraints:
    """Migration 0009 (B-12, D-25): one stored report per disposition, submitted at most once."""

    SHA = "a" * 64

    @pytest.fixture
    def reviewer(self, conn):
        return insert_user(conn, role="reviewer")

    @pytest.fixture
    def event_id(self, conn, reviewer):
        event_id = uuid.uuid4()
        assert insert_event(conn, id=event_id)
        conn.execute(
            text(
                "INSERT INTO disposition (event_id, seq, tier, previous_tier, reviewer_id,"
                " rationale, recorded_at, deadline_utc) VALUES (:e, 1, 'DEATH', NULL, :r, NULL,"
                " :t, :d)"
            ),
            {"e": event_id, "r": reviewer, "t": T0, "d": T0 + timedelta(days=10)},
        )
        return event_id

    def report(self, conn, event, reviewer, **values) -> bool:
        row = {
            "id": uuid.uuid4(),  # report_id, contract delta \u03944 (migration 0017)
            "event_id": event,
            "disposition_seq": 1,
            "document": "{}",
            "report_sha256": self.SHA,
            "assembled_at": T0,
            "assembled_by": reviewer,
            "submitted_at": None,
            "submitted_by": None,
            "submission_note": None,
            **values,
        }
        cols = ", ".join(row)
        params = ", ".join(f":{k}" for k in row)
        return not violates(conn, f"INSERT INTO report ({cols}) VALUES ({params})", row)

    def test_assembled_and_submitted_reports_accepted(self, conn, event_id, reviewer):
        assert self.report(conn, event_id, reviewer)
        conn.execute(
            text(
                "UPDATE report SET submitted_at = :t, submitted_by = :r, submission_note = 'sent'"
                " WHERE event_id = :e"
            ),
            {"t": T0, "r": reviewer, "e": event_id},
        )

    def test_one_report_per_disposition(self, conn, event_id, reviewer):
        assert self.report(conn, event_id, reviewer)
        assert not self.report(conn, event_id, reviewer, report_sha256="b" * 64)

    @pytest.mark.parametrize(
        "values",
        [
            {"disposition_seq": 2},  # no such disposition
            {"report_sha256": "A" * 64},
            {"report_sha256": "a" * 63},
            {"document": None},
            {"assembled_at": None},
            {"assembled_by": uuid.uuid4()},  # no such user
            {"submitted_at": T0},  # without the reviewer who submitted
            {"submission_note": "sent"},  # a note only comes with a submission
        ],
    )
    def test_invalid_rows_rejected(self, conn, event_id, reviewer, values):
        assert not self.report(conn, event_id, reviewer, **values)

    def test_reported_disposition_cannot_be_deleted(self, conn, event_id, reviewer):
        assert self.report(conn, event_id, reviewer)
        assert violates(conn, "DELETE FROM disposition WHERE event_id = :e", {"e": event_id})


def test_timestamps_round_trip_as_utc(conn):
    value = conn.execute(
        text("SELECT sampled_at FROM metric_sample WHERE system_id = :s"), {"s": SYS}
    ).scalar()
    assert value == T0 and value.utcoffset() == timedelta(0)


# --- migration 0003: gaps, accounts, ledger, notifications ------------------------------------


class TestGapConstraints:
    """REQ-02 gap records and INV-12 (gap events never use review states)."""

    OPEN = {**GAP, "state": "GAP_OPEN", "gap_ended_at": None}

    def test_open_and_closed_gaps_accepted(self, conn):
        assert insert_event(conn, **self.OPEN)
        assert insert_event(conn, **GAP)

    @pytest.mark.parametrize(
        "change",
        [
            {"state": "FLAGGED"},  # INV-12
            {"state": "UNDER_REVIEW"},
            {"state": "DISPOSITIONED"},
            {"gap_started_at": None},
            {"state": "GAP_CLOSED", "gap_ended_at": None},
            {"gap_ended_at": T0},  # open gap with an end time
            {"state": "GAP_CLOSED", "gap_ended_at": T0 - timedelta(hours=1)},  # ends before start
        ],
    )
    def test_invalid_gap_rejected(self, conn, change):
        assert not insert_event(conn, **{**self.OPEN, **change})

    @pytest.mark.parametrize("state", ["GAP_OPEN", "REVIEWER_NOTIFIED", "GAP_CLOSED"])
    def test_breach_cannot_use_gap_states(self, conn, state):
        assert not insert_event(conn, state=state)

    def test_breach_cannot_carry_gap_fields(self, conn):
        assert not insert_event(conn, gap_started_at=T0)

    @pytest.mark.parametrize("second", ["GAP_OPEN", "REVIEWER_NOTIFIED"])
    def test_one_open_gap_per_system(self, conn, second):
        assert insert_event(conn, **self.OPEN)
        assert not insert_event(conn, **{**self.OPEN, "state": second})
        assert insert_event(conn, **GAP)  # closed gaps don't count

    def test_gap_timeout_must_be_positive(self, conn):
        assert violates(
            conn,
            "INSERT INTO monitored_system (id, name, gap_missed_intervals) VALUES (:id, 'g', 0)",
            {"id": uuid.uuid4()},
        )


def insert_user(conn, *, role="viewer", system_id=None, name=None) -> uuid.UUID | None:
    user_id = uuid.uuid4()
    ok = not violates(
        conn,
        "INSERT INTO app_user (id, name, role, system_id) VALUES (:id, :n, :r, :s)",
        {"id": user_id, "n": name or f"u-{user_id.hex[:8]}", "r": role, "s": system_id},
    )
    return user_id if ok else None


class TestAccountConstraints:
    def test_system_accounts_are_scoped_to_one_system(self, conn):
        assert insert_user(conn, role="system", system_id=SYS)
        assert insert_user(conn, role="system") is None
        assert insert_user(conn, role="viewer", system_id=SYS) is None
        assert insert_user(conn, role="superuser") is None

    def test_user_names_are_unique(self, conn):
        assert insert_user(conn, name="alice")
        assert insert_user(conn, name="alice") is None

    def test_key_hash_is_a_unique_sha256(self, conn):
        user_id = insert_user(conn)
        sql = (
            "INSERT INTO api_key (id, user_id, key_hash, key_prefix)"
            " VALUES (:id, :u, :h, 'acm_abc')"
        )
        assert not violates(conn, sql, {"id": uuid.uuid4(), "u": user_id, "h": "a" * 64})
        assert violates(conn, sql, {"id": uuid.uuid4(), "u": user_id, "h": "a" * 64})
        assert violates(conn, sql, {"id": uuid.uuid4(), "u": user_id, "h": "plain-key"})

    def test_threshold_changed_by_must_be_a_user(self, conn):
        sql = (
            "INSERT INTO threshold_version (id, system_id, metric_name, metric_type, direction,"
            " limit_value, valid_from, changed_by) VALUES (:id, :s, 'q', 'drift', 'above', 1,"
            " :vf, :by)"
        )
        assert violates(conn, sql, {"id": uuid.uuid4(), "s": SYS, "vf": T0, "by": uuid.uuid4()})
        user_id = insert_user(conn, role="admin")
        assert not violates(conn, sql, {"id": uuid.uuid4(), "s": SYS, "vf": T0, "by": user_id})


def insert_ledger(conn, seq, prev_hash, *, entry_type="VIEW", event_id=None) -> bool:
    return not violates(
        conn,
        "INSERT INTO ledger_entry (seq, entry_type, event_id, actor_id, at, payload, prev_hash,"
        " entry_hash) VALUES (:seq, :t, :e, NULL, :at, '{}', :p, :h)",
        {"seq": seq, "t": entry_type, "e": event_id, "at": T0, "p": prev_hash, "h": "b" * 64},
    )


class TestLedgerConstraints:
    def test_chain_cannot_fork(self, conn):
        assert insert_ledger(conn, 1, "0" * 64)
        assert not insert_ledger(conn, 2, "0" * 64)  # same predecessor twice

    @pytest.mark.parametrize(
        ("seq", "prev", "entry_type"),
        [(0, "0" * 64, "VIEW"), (1, "xyz", "VIEW"), (1, "0" * 64, "EDITED")],
    )
    def test_checks(self, conn, seq, prev, entry_type):
        assert not insert_ledger(conn, seq, prev, entry_type=entry_type)

    @pytest.mark.parametrize(
        "entry_type", ["DISPOSITION", "REPORT_ASSEMBLED", "REPORT_SUBMITTED", "REFUSED"]
    )
    def test_review_entries_accepted(self, conn, entry_type):
        assert insert_ledger(conn, 1, "0" * 64, entry_type=entry_type)

    def test_audited_event_cannot_be_deleted(self, conn):
        event_id = uuid.uuid4()
        assert insert_event(conn, id=event_id)
        assert insert_ledger(conn, 1, "0" * 64, entry_type="FLAGGED", event_id=event_id)
        assert violates(conn, "DELETE FROM event WHERE id = :id", {"id": event_id})


class TestNotificationConstraints:
    @pytest.fixture
    def event_id(self, conn):
        event_id = uuid.uuid4()
        assert insert_event(conn, id=event_id)
        return event_id

    def insert_notification(self, conn, event_id, **overrides) -> uuid.UUID | None:
        values = {
            "id": uuid.uuid4(),
            "e": event_id,
            "t": "flag_notification",
            "c": T0,
            "d": T0 + timedelta(minutes=15),
            "p": None,
            "rd": None,
        }
        values.update(overrides)
        ok = not violates(
            conn,
            "INSERT INTO notification (id, event_id, notification_type, payload, created_at,"
            " deliver_by, reminder_percent, reminder_deadline_utc)"
            " VALUES (:id, :e, :t, '{}', :c, :d, :p, :rd)",
            values,
        )
        return values["id"] if ok else None

    def test_notification_checks(self, conn, event_id):
        assert self.insert_notification(conn, event_id)
        assert self.insert_notification(conn, event_id, t="sms") is None
        assert self.insert_notification(conn, event_id, d=T0 - timedelta(seconds=1)) is None
        assert self.insert_notification(conn, uuid.uuid4()) is None  # unknown event

    def test_reminder_checks(self, conn, event_id):
        """Migration 0008 (B-11): a reminder names its checkpoint and deadline; each checkpoint
        once per event."""
        deadline = T0 + timedelta(days=10)
        reminder = {"t": "reminder", "rd": deadline}
        assert self.insert_notification(conn, event_id, p=50, **reminder)
        assert self.insert_notification(conn, event_id, p=50, **reminder) is None  # once
        assert self.insert_notification(conn, event_id, p=80, **reminder)
        assert self.insert_notification(conn, event_id, p=60, **reminder) is None
        assert self.insert_notification(conn, event_id, t="reminder") is None  # no checkpoint
        assert self.insert_notification(conn, event_id, p=100, t="reminder") is None  # no deadline
        assert self.insert_notification(conn, event_id, p=100, rd=deadline) is None  # a flag
        # Flag notifications aren't limited: the index only covers reminders.
        assert self.insert_notification(conn, event_id) and self.insert_notification(conn, event_id)

    @pytest.mark.parametrize(
        ("types", "ok"),
        [("{flag_notification}", True), ("{}", False), ("{flag_notification,sms}", False)],
    )
    def test_webhook_event_types(self, conn, types, ok):
        sql = (
            "INSERT INTO webhook (id, url, event_types, secret, created_at)"
            " VALUES (:id, 'https://h.example', CAST(:t AS text[]), 's', :c)"
        )
        assert violates(conn, sql, {"id": uuid.uuid4(), "t": types, "c": T0}) is not ok

    @pytest.mark.parametrize(
        ("status", "delivered_at", "ok"),
        [
            ("PENDING", None, True),
            ("DELIVERED", T0, True),
            ("DELIVERED", None, False),
            ("FAILED", T0, False),
            ("SENT", None, False),
        ],
    )
    def test_delivery_status(self, conn, event_id, status, delivered_at, ok):
        notification_id = self.insert_notification(conn, event_id)
        webhook_id = uuid.uuid4()
        conn.execute(
            text(
                "INSERT INTO webhook (id, url, event_types, secret, created_at)"
                " VALUES (:id, 'https://h.example', '{flag_notification}', 's', :c)"
            ),
            {"id": webhook_id, "c": T0},
        )
        sql = (
            "INSERT INTO notification_delivery (notification_id, webhook_id, status, attempts,"
            " next_attempt_at, delivered_at) VALUES (:n, :w, :s, 0, :t, :d)"
        )
        params = {"n": notification_id, "w": webhook_id, "s": status, "t": T0, "d": delivered_at}
        assert violates(conn, sql, params) is not ok


@pytest.mark.parametrize("table", ["simulated_clock", "worker_heartbeat"])
def test_single_row_tables(conn, table):
    columns = "id, offset_seconds" if table == "simulated_clock" else "id, worker_id, beat_at"
    values = "2, 0" if table == "simulated_clock" else "2, 'w', now()"
    assert violates(conn, f"INSERT INTO {table} ({columns}) VALUES ({values})")
