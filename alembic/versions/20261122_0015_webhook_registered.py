"""ledger_entry: WEBHOOK_REGISTERED entries (contract delta Δ8)

Webhook registrations are audited (contract delta Δ8, D-29). The payload carries the URL's
SHA-256 (`url_sha256`), never the URL. No table changes.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-22
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENTRY_TYPES_0014 = (
    "'FLAGGED', 'VIEW', 'GAP_OPENED', 'GAP_CLOSED', 'NOTIFIED', 'DISPOSITION', "
    "'REPORT_ASSEMBLED', 'REPORT_SUBMITTED', 'REFUSED', 'PERSONAL_DATA_ERASED', 'RECORD_ERASED'"
)
ENTRY_TYPES_0015 = ENTRY_TYPES_0014 + ", 'WEBHOOK_REGISTERED'"


def _entry_type_check(values: str) -> None:
    op.drop_constraint(op.f("ck_ledger_entry_entry_type"), "ledger_entry", type_="check")
    op.create_check_constraint(
        op.f("ck_ledger_entry_entry_type"), "ledger_entry", f"entry_type IN ({values})"
    )


def upgrade() -> None:
    _entry_type_check(ENTRY_TYPES_0015)


def downgrade() -> None:
    # Fails if WEBHOOK_REGISTERED entries exist: the ledger is append-only.
    _entry_type_check(ENTRY_TYPES_0014)
