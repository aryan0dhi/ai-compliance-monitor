"""disposition: tier assignments and revisions of breach events

REQ-08a/b, REQ-09 (B-08, B-09): each POST /v1/events/{id}/disposition inserts one row; a revision
is a new row naming the tier it replaced, so nothing is overwritten. Adds the DISPOSITION ledger
entry type. See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
TIERS = "'DEATH', 'CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD', 'OTHER_SERIOUS', 'NOT_SERIOUS'"
ENTRY_TYPES_0003 = "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED'"
ENTRY_TYPES_0006 = ENTRY_TYPES_0003 + ", 'DISPOSITION'"


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def upgrade() -> None:
    op.create_table(
        "disposition",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("previous_tier", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.Uuid(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("recorded_at", TZ, nullable=False),
        sa.CheckConstraint(f"tier IN ({TIERS})", name=op.f("ck_disposition_tier")),
        sa.CheckConstraint(
            f"previous_tier IN ({TIERS})", name=op.f("ck_disposition_previous_tier")
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_disposition_seq_positive")),
        sa.CheckConstraint(
            "(seq = 1) = (previous_tier IS NULL)",
            name=op.f("ck_disposition_previous_tier_matches_seq"),
        ),
        sa.CheckConstraint(
            "tier <> 'NOT_SERIOUS' OR btrim(coalesce(rationale, '')) <> ''",
            name=op.f("ck_disposition_not_serious_has_rationale"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_disposition_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["app_user.id"],
            name=op.f("fk_disposition_reviewer_id_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", "seq", name=op.f("pk_disposition")),
    )
    _entry_type_check(ENTRY_TYPES_0006)


def downgrade() -> None:
    # Fails if DISPOSITION entries exist: the ledger is append-only, so they can't be dropped.
    _entry_type_check(ENTRY_TYPES_0003)
    op.drop_table("disposition")
