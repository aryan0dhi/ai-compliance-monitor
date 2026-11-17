"""harm_field: the four Art. 3(49) fields of each breach event

REQ-07 (B-07, D-20): one row per (event, field), created PENDING when the event is flagged and
confirmed by a reviewer later (REQ-08a). Breach events flagged before this migration have no
rows, and GET /v1/events/{id} shows harm_fields as null for them. See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "harm_field",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="PENDING", nullable=False),
        sa.Column("prefill_evidence", sa.Text(), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.Uuid(), nullable=True),
        sa.Column("confirmed_at", TZ, nullable=True),
        sa.CheckConstraint(
            "name IN ('health_safety', 'critical_infrastructure', 'fundamental_rights', "
            "'property_environment')",
            name=op.f("ck_harm_field_name"),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'CONFIRMED', 'NOT_APPLICABLE')",
            name=op.f("ck_harm_field_status"),
        ),
        sa.CheckConstraint(
            "(status = 'PENDING') = (confirmed_at IS NULL)",
            name=op.f("ck_harm_field_confirmed_at_matches_status"),
        ),
        sa.CheckConstraint(
            "status <> 'PENDING' OR confirmed_by IS NULL",
            name=op.f("ck_harm_field_pending_has_no_reviewer"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_harm_field_event_id_event"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["confirmed_by"],
            ["app_user.id"],
            name=op.f("fk_harm_field_confirmed_by_app_user"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", "name", name=op.f("pk_harm_field")),
    )


def downgrade() -> None:
    op.drop_table("harm_field")
