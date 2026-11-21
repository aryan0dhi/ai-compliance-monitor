"""disposition.deadline_utc: the Article 73 deadline after each disposition

REQ-10 (B-10, D-23): every disposition records the deadline in force after it; the latest row's
value is the event's deadline_utc. Rows written before this migration are backfilled by replaying
each event's dispositions with the B-10 rules (a revision to a less strict tier is treated as if
it had a rationale, because 0006 didn't require one). See docs/database.md.

Written by hand; kept in sync with the models by tests/test_migrations.py (offline) and
tests/integration/test_schema.py (autogenerate against PostgreSQL).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-17
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

import sqlalchemy as sa
from alembic import context, op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of app.modules.review.deadlines as of this migration.
DAYS = {"CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2, "DEATH": 10, "OTHER_SERIOUS": 15}


def _strictness(tier: str) -> float:
    return DAYS.get(tier, float("inf"))


def _backfill() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT event_id, seq, tier, recorded_at FROM disposition ORDER BY event_id, seq")
    ).all()
    current: dict = {}  # event_id -> (tier, anchor, deadline)
    for event_id, seq, tier, at in rows:
        prev_tier, anchor, deadline = current.get(event_id, (None, None, None))
        if anchor is None and tier in DAYS:
            anchor = at
        target: datetime | None = (
            anchor + timedelta(days=DAYS[tier]) if anchor is not None and tier in DAYS else None
        )
        if prev_tier is None or _strictness(tier) < _strictness(prev_tier):
            deadline = target if deadline is None or target is None else min(deadline, target)
        elif _strictness(tier) > _strictness(prev_tier):
            deadline = target
        current[event_id] = (tier, anchor, deadline)
        conn.execute(
            sa.text("UPDATE disposition SET deadline_utc = :d WHERE event_id = :e AND seq = :s"),
            {"d": deadline, "e": event_id, "s": seq},
        )


def upgrade() -> None:
    op.add_column(
        "disposition", sa.Column("deadline_utc", sa.DateTime(timezone=True), nullable=True)
    )
    # Offline (--sql) output has no rows to read; it is only used to compare DDL in tests.
    if not context.is_offline_mode():
        _backfill()
    op.create_check_constraint(
        op.f("ck_disposition_deadline_matches_tier"),
        "disposition",
        "(tier = 'NOT_SERIOUS') = (deadline_utc IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_disposition_deadline_matches_tier"), "disposition", type_="check")
    op.drop_column("disposition", "deadline_utc")
