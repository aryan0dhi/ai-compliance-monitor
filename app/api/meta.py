"""GET /v1/meta: interface version and implemented phases (pod interface, docs/pod-interface).

Unauthenticated, so a cross-team harness can decide which checks apply before it has keys.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field

# docs/pod-interface/openapi.yaml info.version.
INTERFACE_VERSION = "1.0.0-draft"
# Phases whose pod-interface operations this service provides. Phase 4 (review, disposition,
# deadlines, reminders) since #67. Phase 5 since #72: the Δ1 report and its PDF (D-30), report
# versions and submissions (Δ3, Δ4), erasure and the end-of-retention job (REQ-16, Δ6), and the
# scheduled ledger check (Δ5). The event's own evidence hash stays open (D-05).
IMPLEMENTED_PHASES = [3, 4, 5]


class MetaOut(BaseModel):
    interface_version: str
    phases: list[int]
    test_routes: bool
    build: str = Field(
        description="Git commit the running image was built from, or `unknown` (optional in "
        "the pod interface; lets testers tell a defect from an old deployment)."
    )


def build_meta_router(*, test_routes: bool, build: str = "unknown") -> APIRouter:
    router = APIRouter(tags=["discovery"])

    @router.get("/meta", response_model=MetaOut)
    def meta() -> MetaOut:
        return MetaOut(
            interface_version=INTERFACE_VERSION,
            phases=IMPLEMENTED_PHASES,
            test_routes=test_routes,
            build=build,
        )

    return router
