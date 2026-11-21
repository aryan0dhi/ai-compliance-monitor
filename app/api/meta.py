"""GET /v1/meta: interface version and implemented phases (pod interface, docs/pod-interface).

Unauthenticated, so a cross-team harness can decide which checks apply before it has keys.
"""

from fastapi import APIRouter
from pydantic import BaseModel

# docs/pod-interface/openapi.yaml info.version.
INTERFACE_VERSION = "1.0.0-draft"
# Phases whose pod-interface operations this service provides. Phase 4 (review, disposition,
# deadlines, reminders) since #67; the Phase 5 report endpoints exist, but retention, evidence
# hashing, crypto-shredding and the nightly alert don't yet (#72).
IMPLEMENTED_PHASES = [3, 4]


class MetaOut(BaseModel):
    interface_version: str
    phases: list[int]
    test_routes: bool


def build_meta_router(*, test_routes: bool) -> APIRouter:
    router = APIRouter(tags=["discovery"])

    @router.get("/meta", response_model=MetaOut)
    def meta() -> MetaOut:
        return MetaOut(
            interface_version=INTERFACE_VERSION,
            phases=IMPLEMENTED_PHASES,
            test_routes=test_routes,
        )

    return router
