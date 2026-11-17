"""harm fields a monitored system can affect

REQ-07 (D-20): `monitored_system.harm_fields` lists the Art. 3(49) fields whose evidence is
prefilled when the system breaches a threshold. Existing systems get an empty list (nothing
prefilled). See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "monitored_system",
        sa.Column(
            "harm_fields",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_monitored_system_harm_fields_known"),
        "monitored_system",
        "harm_fields <@ ARRAY['health_safety', 'critical_infrastructure', 'fundamental_rights', "
        "'property_environment']::text[]",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_monitored_system_harm_fields_known"), "monitored_system", type_="check"
    )
    op.drop_column("monitored_system", "harm_fields")
