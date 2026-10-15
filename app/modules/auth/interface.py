"""Authentication interface used by every protected endpoint.

Contract (Phase 2, Section 5): `Authorization: Bearer <api_key>`. Monitored-system keys are scoped
to one system_id and may only submit metrics for it; human-user keys carry a viewer, reviewer or
admin role.

NOT IMPLEMENTED. `get_principal` fails closed: it rejects every request, so no protected endpoint
can be reached until the auth module provides a real implementation.
"""

from enum import StrEnum
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel


class Role(StrEnum):
    SYSTEM = "system"
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    ADMIN = "admin"


class Principal(BaseModel):
    actor_id: UUID
    role: Role
    # Set only for monitored-system keys (Role.SYSTEM).
    system_id: UUID | None = None


def get_principal() -> Principal:
    """FastAPI dependency returning the authenticated caller. Fails closed until implemented."""
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication is not implemented yet (REQ-13; owner: app/modules/auth). "
        "All protected endpoints are denied.",
        headers={"WWW-Authenticate": "Bearer"},
    )
