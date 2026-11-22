"""Migrations must match the models. Runs Alembic in offline (--sql) mode, so no database needed.

Once Docker/PostgreSQL is available, an online autogenerate-diff check should replace the DDL
comparison below (backlog).
"""

import io
import re
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.db.models import Base

ROOT = Path(__file__).resolve().parents[1]


def _config(buf: io.StringIO | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"), output_buffer=buf)
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def _offline_sql(direction: str, target: str) -> str:
    buf = io.StringIO()
    getattr(command, direction)(_config(buf), target, sql=True)
    return buf.getvalue()


def _create_table_lines(sql: str, table: str) -> set[str]:
    match = re.search(rf"CREATE TABLE {table} \((.*?)\n\)\s*;", sql, re.DOTALL)
    assert match, f"CREATE TABLE {table} not found"
    return {line.strip().rstrip(",") for line in match.group(1).splitlines() if line.strip()}


_ALTER_TABLE = re.compile(r"^ALTER TABLE (\w+) (.+?);$", re.MULTILINE | re.DOTALL)


def _table_lines_after_migrations(sql: str, table: str) -> set[str]:
    """The table's CREATE TABLE lines with every later ALTER TABLE applied, in order."""
    lines = _create_table_lines(sql, table)
    for match in _ALTER_TABLE.finditer(sql):
        if match.group(1) != table:
            continue
        action = " ".join(match.group(2).split())
        if action.startswith("ADD COLUMN "):
            lines.add(action.removeprefix("ADD COLUMN "))
        elif action.startswith("DROP COLUMN "):
            column = action.split()[2]
            lines = {line for line in lines if not line.startswith(f"{column} ")}
        elif action.startswith("ADD CONSTRAINT "):
            lines.add("CONSTRAINT " + action.removeprefix("ADD CONSTRAINT "))
        elif action.startswith("DROP CONSTRAINT "):
            name = action.split()[2]
            lines = {line for line in lines if not line.startswith(f"CONSTRAINT {name} ")}
        elif re.fullmatch(r"ALTER COLUMN \w+ (DROP|SET) NOT NULL", action):
            _, _, column, change, *_ = action.split()
            old = next(line for line in lines if line.startswith(f"{column} "))
            base = old.removesuffix(" NOT NULL")
            lines = (lines - {old}) | {base if change == "DROP" else base + " NOT NULL"}
        else:
            raise AssertionError(f"test can't replay: ALTER TABLE {table} {action}")
    return lines


def _create_index_statements(sql: str) -> set[str]:
    return {m.strip() for m in re.findall(r"(CREATE (?:UNIQUE )?INDEX [^;]+);", sql)}


def test_single_head():
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert heads == ["0015"], heads


@pytest.fixture(scope="module")
def upgrade_sql() -> str:
    return _offline_sql("upgrade", "head")


@pytest.fixture(scope="module")
def model_ddl() -> tuple[dict[str, str], set[str]]:
    dialect = postgresql.dialect()
    tables = {
        t.name: str(CreateTable(t).compile(dialect=dialect)) for t in Base.metadata.sorted_tables
    }
    indexes = {
        str(CreateIndex(ix).compile(dialect=dialect)).strip()
        for t in Base.metadata.sorted_tables
        for ix in t.indexes
    }
    return tables, indexes


@pytest.mark.parametrize("table", sorted(Base.metadata.tables))
def test_migration_table_matches_model(table, upgrade_sql, model_ddl):
    tables, _ = model_ddl
    model_lines = _create_table_lines(tables[table] + ";", table)
    migration_lines = _table_lines_after_migrations(upgrade_sql, table)
    assert migration_lines == model_lines, {
        "only_in_migration": migration_lines - model_lines,
        "only_in_model": model_lines - migration_lines,
    }


def test_migration_indexes_match_model(upgrade_sql, model_ddl):
    _, model_indexes = model_ddl
    assert _create_index_statements(upgrade_sql) == model_indexes


def test_downgrade_drops_everything():
    sql = _offline_sql("downgrade", "0002:0001")
    for table in ["event", "threshold_version", "metric_sample", "monitored_system"]:
        assert f"DROP TABLE {table};" in sql


def test_0015_adds_the_webhook_registered_entry_type():
    up = _offline_sql("upgrade", "0014:0015")
    assert "'RECORD_ERASED', 'WEBHOOK_REGISTERED')" in up
    down = _offline_sql("downgrade", "0015:0014")
    assert "'WEBHOOK_REGISTERED'" not in down


def test_0014_adds_the_record_erased_entry_type():
    up = _offline_sql("upgrade", "0013:0014")
    assert "'PERSONAL_DATA_ERASED', 'RECORD_ERASED')" in up
    down = _offline_sql("downgrade", "0014:0013")
    assert "'RECORD_ERASED'" not in down


def test_0013_adds_the_event_retention_guard():
    up = _offline_sql("upgrade", "0012:0013")
    assert "CREATE FUNCTION event_retention_guard()" in up
    assert "BEFORE DELETE OR UPDATE OF retain_until ON event" in up
    down = _offline_sql("downgrade", "0013:0012")
    assert "DROP TRIGGER event_retention_guard ON event" in down
    assert "DROP FUNCTION event_retention_guard()" in down


def test_0012_adds_record_key_and_the_erasure_entry_type():
    up = _offline_sql("upgrade", "0011:0012")
    assert "CREATE TABLE record_key" in up
    assert "'REFUSED', 'PERSONAL_DATA_ERASED')" in up
    down = _offline_sql("downgrade", "0012:0011")
    assert "DROP TABLE record_key;" in down
    assert "'PERSONAL_DATA_ERASED'" not in down


def test_0011_adds_ledger_check_and_ledger_alerts():
    up = _offline_sql("upgrade", "0010:0011")
    assert "CREATE TABLE ledger_check" in up
    assert "ALTER TABLE notification ALTER COLUMN event_id DROP NOT NULL;" in up
    assert "'reminder', 'ledger_alert')" in up
    down = _offline_sql("downgrade", "0011:0010")
    assert "DROP TABLE ledger_check;" in down
    assert "ALTER TABLE notification ALTER COLUMN event_id SET NOT NULL;" in down
    assert "'ledger_alert'" not in down


def test_0010_adds_the_refused_entry_type():
    up = _offline_sql("upgrade", "0009:0010")
    assert "'REPORT_SUBMITTED', 'REFUSED')" in up
    down = _offline_sql("downgrade", "0010:0009")
    assert "'REFUSED'" not in down


def test_downgrade_0009_drops_report_and_its_ledger_types():
    sql = _offline_sql("downgrade", "0009:0008")
    assert "DROP TABLE report;" in sql
    assert "'NOTIFIED', 'DISPOSITION'))" in sql
    assert "'REPORT_ASSEMBLED'" not in sql


def test_upgrade_0009_allows_report_ledger_entries():
    sql = _offline_sql("upgrade", "0008:0009")
    assert "'DISPOSITION', 'REPORT_ASSEMBLED', 'REPORT_SUBMITTED')" in sql


def test_downgrade_0008_drops_the_reminder_columns():
    sql = _offline_sql("downgrade", "0008:0007")
    assert "DROP INDEX uq_notification_reminder_once;" in sql
    for column in ("reminder_percent", "reminder_deadline_utc"):
        assert f"ALTER TABLE notification DROP COLUMN {column};" in sql


def test_downgrade_0007_drops_the_deadline():
    sql = _offline_sql("downgrade", "0007:0006")
    assert "ALTER TABLE disposition DROP CONSTRAINT ck_disposition_deadline_matches_tier;" in sql
    assert "ALTER TABLE disposition DROP COLUMN deadline_utc;" in sql


def test_downgrade_0006_drops_disposition_and_its_ledger_type():
    sql = _offline_sql("downgrade", "0006:0005")
    assert "DROP TABLE disposition;" in sql
    assert "entry_type IN ('FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED'))" in sql
    assert "'DISPOSITION'" not in sql


def test_upgrade_0006_allows_disposition_ledger_entries():
    sql = _offline_sql("upgrade", "0005:0006")
    assert "'NOTIFIED', 'DISPOSITION')" in sql


def test_downgrade_0005_drops_harm_field():
    assert "DROP TABLE harm_field;" in _offline_sql("downgrade", "0005:0004")


def test_downgrade_0004_drops_system_harm_fields():
    sql = _offline_sql("downgrade", "0004:0003")
    constraint = "ck_monitored_system_harm_fields_known"
    assert f"ALTER TABLE monitored_system DROP CONSTRAINT {constraint};" in sql
    assert "ALTER TABLE monitored_system DROP COLUMN harm_fields;" in sql


def test_downgrade_0003_drops_its_tables_and_columns():
    sql = _offline_sql("downgrade", "0003:0002")
    for table in [
        "app_user",
        "api_key",
        "ledger_entry",
        "webhook",
        "notification",
        "notification_delivery",
        "simulated_clock",
        "worker_heartbeat",
    ]:
        assert f"DROP TABLE {table};" in sql
    for table, column in [
        ("event", "gap_started_at"),
        ("event", "gap_ended_at"),
        ("monitored_system", "gap_missed_intervals"),
    ]:
        assert f"ALTER TABLE {table} DROP COLUMN {column};" in sql
