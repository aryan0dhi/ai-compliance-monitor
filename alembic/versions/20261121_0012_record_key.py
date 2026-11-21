"""record_key and PERSONAL_DATA_ERASED: erasable personal data (REQ-16)

REQ-16, AC-18 (#70, D-28): each event with personal data gets a data key wrapped by
RECORD_KEY_MASTER_KEY. Personal free text (disposition rationale, reviewer evidence, report
document and submission note) is stored sealed with that key; destroying the key at the end of
retention makes it unreadable while every row and ledger hash stays. See docs/database.md.

Data migration: rows written before this revision hold plaintext. Upgrade seals them in place,
creating a key for each event that needs one, using the master key the app is configured with
(the public development key if RECORD_KEY_MASTER_KEY is unset outside production). Ledger
entries written before this revision keep their plaintext payloads: the ledger is append-only
and their hashes cover that text.

Downgrade opens the sealed values again; it refuses once any key has been destroyed, because
that data no longer exists in readable form.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-21
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from alembic import context, op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_0010 = (
    "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED', 'DISPOSITION', "
    "'REPORT_ASSEMBLED', 'REPORT_SUBMITTED', 'REFUSED'"
)
ENTRY_TYPES_0012 = ENTRY_TYPES_0010 + ", 'PERSONAL_DATA_ERASED'"

# (table, column, key columns, purpose) for every personal-data column. The purposes match
# app.modules.records.service.
_COLUMNS = (
    (
        "disposition",
        "rationale",
        ("event_id", "seq"),
        lambda row: f"disposition.rationale:{row[1]}",
    ),
    ("harm_field", "evidence", ("event_id", "name"), lambda row: f"harm_field.evidence:{row[1]}"),
    (
        "report",
        "document",
        ("event_id", "disposition_seq"),
        lambda row: f"report.document:{row[1]}",
    ),
    (
        "report",
        "submission_note",
        ("event_id", "disposition_seq"),
        lambda row: f"report.submission_note:{row[1]}",
    ),
)


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def _rows(conn: sa.Connection, table: str, column: str, keys: tuple[str, ...]) -> list[sa.Row]:
    cols = ", ".join((*keys, column))
    return list(conn.execute(sa.text(f"SELECT {cols} FROM {table} WHERE {column} IS NOT NULL")))


def _update(
    conn: sa.Connection, table: str, column: str, keys: tuple[str, ...], row: sa.Row, value: str
) -> None:
    where = " AND ".join(f"{k} = :k{i}" for i, k in enumerate(keys))
    params = {f"k{i}": row[i] for i in range(len(keys))}
    conn.execute(sa.text(f"UPDATE {table} SET {column} = :v WHERE {where}"), {**params, "v": value})


def upgrade() -> None:
    op.create_table(
        "record_key",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("wrapped_key", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("destroyed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(wrapped_key IS NULL) = (destroyed_at IS NOT NULL)",
            name=op.f("ck_record_key_destroyed_has_no_key"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_record_key_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_record_key")),
    )
    _entry_type_check(ENTRY_TYPES_0012)
    if context.is_offline_mode():
        # `alembic upgrade --sql` can't read rows; sealing existing data needs a live database.
        op.execute("-- 0012: existing personal data is sealed only when run against a database")
        return

    from app.modules.records.crypto import is_token, new_data_key, seal, wrap_key
    from app.modules.records.service import master_key

    conn = op.get_bind()
    keys: dict[UUID, bytes] = {}
    now = datetime.now(UTC)
    for table, column, key_cols, purpose in _COLUMNS:
        for row in _rows(conn, table, column, key_cols):
            value = row[len(key_cols)]
            if is_token(value):
                continue
            event_id = row[0]
            if event_id not in keys:
                keys[event_id] = new_data_key()
                conn.execute(
                    sa.text(
                        "INSERT INTO record_key (event_id, wrapped_key, created_at) "
                        "VALUES (:e, :w, :t)"
                    ),
                    {
                        "e": event_id,
                        "w": wrap_key(master_key(), event_id, keys[event_id]),
                        "t": now,
                    },
                )
            _update(
                conn,
                table,
                column,
                key_cols,
                row,
                seal(keys[event_id], event_id, purpose(row), value),
            )


def downgrade() -> None:
    if context.is_offline_mode():
        op.execute("-- 0012: sealed personal data is opened only when run against a database")
        _entry_type_check(ENTRY_TYPES_0010)
        op.drop_table("record_key")
        return
    from app.modules.records.crypto import is_token, open_token, unwrap_key
    from app.modules.records.service import master_key

    conn = op.get_bind()
    if conn.execute(sa.text("SELECT 1 FROM record_key WHERE destroyed_at IS NOT NULL")).first():
        raise RuntimeError(
            "can't downgrade past 0012: some events' personal data has been erased (REQ-16)"
        )
    keys = {
        event_id: unwrap_key(master_key(), event_id, bytes(wrapped))
        for event_id, wrapped in conn.execute(
            sa.text("SELECT event_id, wrapped_key FROM record_key")
        )
    }
    for table, column, key_cols, purpose in _COLUMNS:
        for row in _rows(conn, table, column, key_cols):
            value = row[len(key_cols)]
            if is_token(value):
                plain = open_token(keys[row[0]], row[0], purpose(row), value)
                _update(conn, table, column, key_cols, row, plain)
    # Fails if PERSONAL_DATA_ERASED entries exist; there are none without a destroyed key.
    _entry_type_check(ENTRY_TYPES_0010)
    op.drop_table("record_key")
