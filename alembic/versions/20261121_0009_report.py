"""report: assembled Article 73 reports and their submission

REQ-12 (B-12, D-25): the first GET /v1/events/{id}/report after a serious-tier disposition stores
the report document, so later calls return the same bytes. One row per disposition reported on,
referencing it with a RESTRICT foreign key. POST /v1/events/{id}/report/submission fills the
submission columns once. Adds the REPORT_ASSEMBLED and REPORT_SUBMITTED ledger entry types.
See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
ENTRY_TYPES_0006 = "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED', 'DISPOSITION'"
ENTRY_TYPES_0009 = ENTRY_TYPES_0006 + ", 'REPORT_ASSEMBLED', 'REPORT_SUBMITTED'"


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def upgrade() -> None:
    op.create_table(
        "report",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("disposition_seq", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("report_sha256", sa.Text(), nullable=False),
        sa.Column("assembled_at", TZ, nullable=False),
        sa.Column("assembled_by", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", TZ, nullable=True),
        sa.Column("submitted_by", sa.Uuid(), nullable=True),
        sa.Column("submission_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "report_sha256 ~ '^[0-9a-f]{64}$'", name=op.f("ck_report_report_sha256_hex")
        ),
        sa.CheckConstraint(
            "(submitted_at IS NULL) = (submitted_by IS NULL)",
            name=op.f("ck_report_submitted_by_matches_at"),
        ),
        sa.CheckConstraint(
            "submitted_at IS NOT NULL OR submission_note IS NULL",
            name=op.f("ck_report_note_needs_submission"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id", "disposition_seq"],
            ["disposition.event_id", "disposition.seq"],
            name=op.f("fk_report_disposition"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["assembled_by"],
            ["app_user.id"],
            name=op.f("fk_report_assembled_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["app_user.id"],
            name=op.f("fk_report_submitted_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", "disposition_seq", name=op.f("pk_report")),
    )
    _entry_type_check(ENTRY_TYPES_0009)


def downgrade() -> None:
    # Fails if REPORT_* entries exist: the ledger is append-only, so they can't be dropped.
    _entry_type_check(ENTRY_TYPES_0006)
    op.drop_table("report")
