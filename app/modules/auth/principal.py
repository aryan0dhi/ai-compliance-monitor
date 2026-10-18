"""The authenticated caller. Kept apart from interface.py so repositories can import it without a
cycle (interface.py depends on the unit of work, which imports the repositories)."""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel


class Role(StrEnum):
    SYSTEM = "system"
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"


# Human roles allowed to read events, the audit log and the ledger (Section 5.1: "viewer can read
# events/audit/ledger but not write"). Monitored-system keys only submit metrics.
READER_ROLES = frozenset({Role.VIEWER, Role.REVIEWER, Role.ADMIN})


class Principal(BaseModel):
    actor_id: UUID
    role: Role
    # Set only for monitored-system keys (Role.SYSTEM).
    system_id: UUID | None = None
