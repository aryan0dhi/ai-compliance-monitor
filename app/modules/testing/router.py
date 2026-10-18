"""/v1/test/* routes (Section 5.1). Mounted only when settings.test_routes_enabled, so they
return 404 in production. Admin keys only (D-13)."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.core.types import UtcDatetime
from app.db.uow import UnitOfWork, get_uow
from app.modules.auth.interface import Principal, Role, get_principal
from app.modules.testing.clock import SimulatedClock, get_simulated_clock
from app.modules.testing.service import reset_test_environment

router = APIRouter(prefix="/test", tags=["test"])

MAX_ADVANCE_S = 10 * 366 * 24 * 3600  # ten years

_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "Admin key required"},
}


class ClockAdvanceIn(BaseModel):
    # A JSON integer only: no numeric strings or booleans (as for metric values, D-06).
    seconds: int = Field(strict=True, ge=0, le=MAX_ADVANCE_S)


class ClockOut(BaseModel):
    simulated_time: UtcDatetime
    offset_seconds: int


def _require_admin(principal: Principal) -> None:
    if principal.role is not Role.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "test routes require an admin key")


@router.post(
    "/clock/advance",
    response_model=ClockOut,
    responses={**_ERRORS, 400: {"description": "seconds must be an integer from 0 to 10 years"}},
)
def advance_clock(
    body: ClockAdvanceIn,
    principal: Principal = Depends(get_principal),
    clock: SimulatedClock = Depends(get_simulated_clock),
) -> ClockOut:
    _require_admin(principal)
    simulated = clock.advance(body.seconds)
    return ClockOut(simulated_time=simulated, offset_seconds=clock.offset_seconds())


@router.get("/clock", response_model=ClockOut, responses=_ERRORS)
def read_clock(
    principal: Principal = Depends(get_principal),
    clock: SimulatedClock = Depends(get_simulated_clock),
) -> ClockOut:
    _require_admin(principal)
    return ClockOut(simulated_time=clock.now(), offset_seconds=clock.offset_seconds())


@router.post("/reset", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
def reset(
    principal: Principal = Depends(get_principal),
    uow: UnitOfWork = Depends(get_uow),
    clock: SimulatedClock = Depends(get_simulated_clock),
) -> Response:
    _require_admin(principal)
    reset_test_environment(uow)
    clock.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
