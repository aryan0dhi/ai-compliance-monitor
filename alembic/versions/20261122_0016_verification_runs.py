"""ledger_check trigger/head_check and reset_counter (contract delta Δ5, A1)

Contract delta Δ5 lists ledger verification runs and lets a reviewer start one, so each
ledger_check row records its `trigger` (SCHEDULED, or MANUAL for POST /v1/ledger/verifications)
and `head_check` (how the chain's end compared with the last passing run: MATCH, MISMATCH or
NO_ANCHOR). Existing rows become SCHEDULED with head_check NULL. A1 adds `reset_count` to
/healthz: reset_counter holds it, outside the tables a reset truncates.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ledger_check",
        sa.Column("trigger", sa.Text(), server_default="SCHEDULED", nullable=False),
    )
    op.add_column("ledger_check", sa.Column("head_check", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_ledger_check_trigger"), "ledger_check", "trigger IN ('SCHEDULED', 'MANUAL')"
    )
    op.create_check_constraint(
        op.f("ck_ledger_check_head_check"),
        "ledger_check",
        "head_check IN ('MATCH', 'MISMATCH', 'NO_ANCHOR')",
    )
    op.create_table(
        "reset_counter",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("resets", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_reset_counter_single_row")),
        sa.CheckConstraint("resets >= 0", name=op.f("ck_reset_counter_resets_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reset_counter")),
    )


def downgrade() -> None:
    op.drop_table("reset_counter")
    op.drop_constraint(op.f("ck_ledger_check_head_check"), "ledger_check", type_="check")
    op.drop_constraint(op.f("ck_ledger_check_trigger"), "ledger_check", type_="check")
    op.drop_column("ledger_check", "head_check")
    op.drop_column("ledger_check", "trigger")
