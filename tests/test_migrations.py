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
        else:
            raise AssertionError(f"test can't replay: ALTER TABLE {table} {action}")
    return lines


def _create_index_statements(sql: str) -> set[str]:
    return {m.strip() for m in re.findall(r"(CREATE (?:UNIQUE )?INDEX [^;]+);", sql)}


def test_single_head():
    heads = ScriptDirectory.from_config(_config()).get_heads()
    assert heads == ["0003"], heads


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
