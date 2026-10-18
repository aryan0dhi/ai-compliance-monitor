"""GET /v1/me: the identity behind the presented key (pod interface, docs/pod-interface)."""

from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.modules.auth.interface import Principal, Role, get_principal

router = APIRouter(tags=["discovery"])


class MeOut(BaseModel):
    # The id audit entries record for this key (AC-15 checks attribution against it).
    actor_id: UUID
    role: Role
    system_id: UUID | None = None


@router.get(
    "/me", response_model=MeOut, responses={401: {"description": "Missing or invalid API key"}}
)
def me(principal: Principal = Depends(get_principal)) -> MeOut:
    return MeOut(actor_id=principal.actor_id, role=principal.role, system_id=principal.system_id)
