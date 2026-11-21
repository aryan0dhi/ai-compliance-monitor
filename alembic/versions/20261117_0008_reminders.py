"""notification.reminder_percent / reminder_deadline_utc: Article 73 reminders

REQ-11 (B-11): a `reminder` outbox row records its checkpoint (50, 80 or 100 % of the window
from A to the deadline) and the deadline it counts towards. A unique partial index sends each
checkpoint once per event. Existing rows are flag notifications, so both columns stay NULL.
See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("notification", sa.Column("reminder_percent", sa.Integer(), nullable=True))
    op.add_column(
        "notification",
        sa.Column("reminder_deadline_utc", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_notification_reminder_fields_match_type"),
        "notification",
        "(notification_type = 'reminder') = (reminder_percent IS NOT NULL)"
        " AND (reminder_percent IS NULL) = (reminder_deadline_utc IS NULL)",
    )
    op.create_check_constraint(
        op.f("ck_notification_reminder_percent_known"),
        "notification",
        "reminder_percent IN (50, 80, 100)",
    )
    op.create_index(
        op.f("uq_notification_reminder_once"),
        "notification",
        ["event_id", "reminder_percent"],
        unique=True,
        postgresql_where=sa.text("reminder_percent IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(op.f("uq_notification_reminder_once"), table_name="notification")
    op.drop_constraint(
        op.f("ck_notification_reminder_percent_known"), "notification", type_="check"
    )
    op.drop_constraint(
        op.f("ck_notification_reminder_fields_match_type"), "notification", type_="check"
    )
    op.drop_column("notification", "reminder_deadline_utc")
    op.drop_column("notification", "reminder_percent")
