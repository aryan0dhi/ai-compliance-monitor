"""Model-level checks that need no database."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.models import Base
from app.modules.events.models import EventState, EventType, retain_until_for
from app.modules.metrics.models import MetricType
from app.modules.thresholds.models import Direction


class TestDirection:
    """REQ-03 / AC-03: strict comparison in both directions; equal never breaches."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("0.35", True), ("0.30", False), ("0.3", False), ("0.29", False), ("0.3000001", True)],
    )
    def test_above(self, value, expected):
        assert Direction.ABOVE.breaches(Decimal(value), Decimal("0.30")) is expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [("0.61", True), ("0.70", False), ("0.7", False), ("0.71", False), ("0.6999999", True)],
    )
    def test_below(self, value, expected):
        # Model 5 Ex-1: accuracy 0.61 vs threshold 0.70 flags.
        assert Direction.BELOW.breaches(Decimal(value), Decimal("0.70")) is expected


class TestRetainUntil:
    """REQ-05 storage: detected_at + 6 calendar months, UTC, clamped like PostgreSQL."""

    def test_simple(self):
        assert retain_until_for(datetime(2026, 9, 27, 20, 0, tzinfo=UTC)) == datetime(
            2027, 3, 27, 20, 0, tzinfo=UTC
        )

    def test_year_rollover(self):
        assert retain_until_for(datetime(2026, 7, 15, tzinfo=UTC)) == datetime(
            2027, 1, 15, tzinfo=UTC
        )

    @pytest.mark.parametrize(
        ("detected", "expected"),
        [
            (datetime(2026, 8, 31, tzinfo=UTC), datetime(2027, 2, 28, tzinfo=UTC)),
            (datetime(2027, 8, 31, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC)),  # leap year
            (datetime(2026, 12, 31, tzinfo=UTC), datetime(2027, 6, 30, tzinfo=UTC)),
        ],
    )
    def test_month_end_clamps(self, detected, expected):
        assert retain_until_for(detected) == expected

    def test_non_utc_input_is_computed_in_utc(self):
        # 2026-08-31 20:30 at UTC-4 is 2026-09-01 00:30 UTC, so +6 months lands in March, not Feb.
        detected = datetime(2026, 8, 31, 20, 30, tzinfo=timezone(timedelta(hours=-4)))
        assert retain_until_for(detected) == datetime(2027, 3, 1, 0, 30, tzinfo=UTC)

    def test_at_least_six_months_of_days(self):
        detected = datetime(2026, 8, 31, tzinfo=UTC)
        assert retain_until_for(detected) - detected >= timedelta(days=181)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError):
            retain_until_for(datetime(2026, 9, 27))


class TestApiNames:
    """Values stored in the database must be the Section 5.2 API strings (D-02, D-03)."""

    def test_metric_type(self):
        assert {m.value for m in MetricType} == {"performance", "drift"}

    def test_event_type(self):
        assert {t.value for t in EventType} == {"threshold_breach", "monitoring_gap"}

    def test_event_state(self):
        assert {s.value for s in EventState} == {
            "FLAGGED",
            "UNDER_REVIEW",
            "DISPOSITIONED",
            "CLOSED",
        }


class TestSchemaShape:
    def test_registry_has_phase3_tables(self):
        assert {"monitored_system", "metric_sample", "threshold_version", "event"} <= set(
            Base.metadata.tables
        )

    def test_metric_sample_primary_key(self):
        pk = Base.metadata.tables["metric_sample"].primary_key
        assert [c.name for c in pk.columns] == ["system_id", "metric_name", "sampled_at"]

    def test_values_are_numeric_not_float(self):
        from sqlalchemy import Float, Numeric

        for table, col in [
            ("metric_sample", "value"),
            ("threshold_version", "limit_value"),
            ("event", "observed_value"),
            ("event", "threshold_value"),
        ]:
            type_ = Base.metadata.tables[table].c[col].type
            assert isinstance(type_, Numeric) and not isinstance(type_, Float), (table, col)

    def test_all_datetimes_are_timezone_aware(self):
        from sqlalchemy import DateTime

        for table in Base.metadata.tables.values():
            for col in table.columns:
                if isinstance(col.type, DateTime):
                    assert col.type.timezone, f"{table.name}.{col.name}"

    def test_foreign_keys_never_cascade(self):
        for table in Base.metadata.tables.values():
            for fk in table.foreign_key_constraints:
                assert fk.ondelete == "RESTRICT", f"{table.name}.{fk.name}"

    def test_event_triggering_sample_fk(self):
        fks = {fk.name: fk for fk in Base.metadata.tables["event"].foreign_key_constraints}
        fk = fks["fk_event_triggering_sample"]
        assert [e.target_fullname for e in fk.elements] == [
            "metric_sample.system_id",
            "metric_sample.metric_name",
            "metric_sample.sampled_at",
        ]
