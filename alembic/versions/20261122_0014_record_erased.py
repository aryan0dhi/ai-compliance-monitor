"""ledger_entry: RECORD_ERASED entries (contract delta A4, Δ6)

The other team's Phase 5 contract delta names the erasure entry type RECORD_ERASED (A4) and adds
erasure on request (Δ6). Both the request and the end-of-retention job now write RECORD_ERASED;
PERSONAL_DATA_ERASED stays allowed for entries written before this revision. No table changes.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_0012 = (
    "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED', 'DISPOSITION', "
    "'REPORT_ASSEMBLED', 'REPORT_SUBMITTED', 'REFUSED', 'PERSONAL_DATA_ERASED'"
)
ENTRY_TYPES_0014 = ENTRY_TYPES_0012 + ", 'RECORD_ERASED'"


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def upgrade() -> None:
    _entry_type_check(ENTRY_TYPES_0014)


def downgrade() -> None:
    # Fails if RECORD_ERASED entries exist: the ledger is append-only, so they can't be dropped.
    _entry_type_check(ENTRY_TYPES_0012)
