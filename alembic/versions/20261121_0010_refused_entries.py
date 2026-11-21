"""ledger_entry: REFUSED entries for refused review calls

Model 5 Ex-4 (B-14, D-26): a disposition or report call on an existing event that is refused with
403 or 409 is recorded in the ledger, so the audit trail shows the attempt. Adds the REFUSED entry
type; no table changes.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-21
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_0009 = (
    "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED', 'DISPOSITION', "
    "'REPORT_ASSEMBLED', 'REPORT_SUBMITTED'"
)
ENTRY_TYPES_0010 = ENTRY_TYPES_0009 + ", 'REFUSED'"


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def upgrade() -> None:
    _entry_type_check(ENTRY_TYPES_0010)


def downgrade() -> None:
    # Fails if REFUSED entries exist: the ledger is append-only, so they can't be dropped.
    _entry_type_check(ENTRY_TYPES_0009)
