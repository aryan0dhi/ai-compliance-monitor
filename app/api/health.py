"""GET /healthz: liveness plus database connectivity (Phase 2, Q7.3).

Unauthenticated so the Lightsail container health check can call it. The worker heartbeat
check will be added by the worker owner.
"""

import logging

from fastapi import APIRouter, Response, status

from app.db.session import check_database

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get("/healthz", responses={503: {"description": "Database unreachable"}})
def healthz(response: Response) -> dict[str, str]:
    try:
        check_database()
        database = "ok"
    except Exception:
        logger.exception("healthz: database check failed")
        database = "unavailable"

    if database != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "database": database}
    return {"status": "ok", "database": database}
