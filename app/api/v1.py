"""Aggregates module routers under /v1 (the pod's common interface, Phase 2 Section 5).

Each module adds its own router here when its endpoints are implemented.

The /v1/test/* routes are only included when `include_test_routes` is true
(`settings.test_routes_enabled`), so they return 404 in production.
"""

from fastapi import APIRouter

from app.api.meta import build_meta_router
from app.modules.auth.router import router as auth_router
from app.modules.events.router import router as events_router
from app.modules.ledger.router import router as ledger_router
from app.modules.metrics.router import router as metrics_router
from app.modules.notifications.router import router as notifications_router
from app.modules.testing.router import router as testing_router


def build_router(*, include_test_routes: bool) -> APIRouter:
    router = APIRouter(prefix="/v1")
    router.include_router(build_meta_router(test_routes=include_test_routes))
    router.include_router(auth_router)
    router.include_router(metrics_router)
    router.include_router(events_router)
    router.include_router(ledger_router)
    router.include_router(notifications_router)
    if include_test_routes:
        router.include_router(testing_router)
    return router
