"""report.id and report_submission (contract delta Δ3, Δ4)

Contract delta Δ4 addresses each stored report version by id (GET /v1/reports/{report_id}) and
lists an event's versions; Δ3 records a submission as its own object that names the report
version it submitted. Existing reports get a random id. Reports marked submitted before this
revision keep their submitted_at and get no report_submission row (they have no report_type).

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report", sa.Column("id", sa.Uuid(), nullable=True))
    op.execute("UPDATE report SET id = gen_random_uuid()")
    op.alter_column("report", "id", nullable=False)
    op.create_unique_constraint(op.f("uq_report_id"), "report", ["id"])
    op.create_table(
        "report_submission",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_by", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submission_kind", sa.Text(), nullable=False),
        sa.Column("report_type", sa.Text(), nullable=False),
        sa.Column("expected_next_report_date", sa.Date(), nullable=True),
        sa.Column("authority_reference", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "submission_kind IN ('INITIAL', 'FOLLOW_UP')",
            name=op.f("ck_report_submission_submission_kind"),
        ),
        sa.CheckConstraint(
            "report_type IN ('Initial', 'Follow up', 'Combined initial and final', "
            "'Final (Reportable incident)')",
            name=op.f("ck_report_submission_report_type"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_report_submission_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["report.id"],
            name=op.f("fk_report_submission_report_id_report"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submitted_by"],
            ["app_user.id"],
            name=op.f("fk_report_submission_submitted_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_report_submission")),
        sa.UniqueConstraint("report_id", name=op.f("uq_report_submission_report_id")),
    )


def downgrade() -> None:
    op.drop_table("report_submission")
    op.drop_constraint(op.f("uq_report_id"), "report", type_="unique")
    op.drop_column("report", "id")
