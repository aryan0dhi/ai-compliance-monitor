"""Aggregates module routers under /v1 (the pod's common interface, Phase 2 Section 5).

Each module adds its own router here when its endpoints are implemented, e.g.:

    from app.modules.metrics.router import router as metrics_router
    router.include_router(metrics_router)

The /v1/test/* routes must only be included when `settings.test_routes_enabled` is true, so
they return 404 in production.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/v1")
