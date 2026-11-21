"""ledger_check and ledger_alert notifications: scheduled ledger verification

REQ-06, Model 5 Ex-5 (#71, D-27): the worker re-verifies the ledger once per interval and records
each result in ledger_check; the first failure after a passing check queues a ledger_alert
notification. A ledger alert isn't about an event, so notification.event_id becomes nullable for
that type only, and webhooks may subscribe to it. See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES_0003 = "'flag_notification', 'reminder'"
TYPES_0011 = TYPES_0003 + ", 'ledger_alert'"


def _type_checks(types: str) -> None:
    op.drop_constraint(op.f("ck_webhook_event_types_known"), "webhook", type_="check")
    op.create_check_constraint(
        op.f("ck_webhook_event_types_known"),
        "webhook",
        f"cardinality(event_types) > 0 AND event_types <@ ARRAY[{types}]::text[]",
    )
    op.drop_constraint(op.f("ck_notification_notification_type"), "notification", type_="check")
    op.create_check_constraint(
        op.f("ck_notification_notification_type"),
        "notification",
        f"notification_type IN ({types})",
    )


def upgrade() -> None:
    op.create_table(
        "ledger_check",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("entries_checked", sa.BigInteger(), nullable=False),
        sa.Column("head_seq", sa.BigInteger(), nullable=False),
        sa.Column("head_hash", sa.Text(), nullable=False),
        sa.Column("first_mismatch", sa.BigInteger(), nullable=True),
        sa.Column("problem", sa.Text(), nullable=True),
        sa.Column("alerted", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "entries_checked >= 0", name=op.f("ck_ledger_check_entries_checked_non_negative")
        ),
        sa.CheckConstraint("head_seq >= 0", name=op.f("ck_ledger_check_head_seq_non_negative")),
        sa.CheckConstraint(
            "head_hash ~ '^[0-9a-f]{64}$'", name=op.f("ck_ledger_check_head_hash_sha256")
        ),
        sa.CheckConstraint(
            "valid = (problem IS NULL)", name=op.f("ck_ledger_check_valid_iff_no_problem")
        ),
        sa.CheckConstraint(
            "valid OR NOT alerted", name=op.f("ck_ledger_check_only_failures_alert")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_check")),
    )
    op.create_index(op.f("ix_ledger_check_checked_at"), "ledger_check", ["checked_at"])
    _type_checks(TYPES_0011)
    op.alter_column("notification", "event_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint(
        op.f("ck_notification_event_id_matches_type"),
        "notification",
        "(event_id IS NULL) = (notification_type = 'ledger_alert')",
    )


def downgrade() -> None:
    # Fails while ledger_alert notifications or subscriptions exist.
    op.drop_constraint(op.f("ck_notification_event_id_matches_type"), "notification", type_="check")
    op.alter_column("notification", "event_id", existing_type=sa.Uuid(), nullable=False)
    _type_checks(TYPES_0003)
    op.drop_index(op.f("ix_ledger_check_checked_at"), table_name="ledger_check")
    op.drop_table("ledger_check")
