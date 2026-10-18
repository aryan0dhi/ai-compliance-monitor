"""GET /healthz: liveness, database connectivity and worker heartbeat (Phase 2, Q7.3).

Unauthenticated so the Lightsail container health check can call it. Only an unreachable
database makes it 503: restarting the API container can't fix a stopped worker, so the worker's
state is reported in the body ("ok", "stale" or "missing") for monitoring instead.
"""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Response, status

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import check_database, get_sessionmaker

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


def classify_heartbeat(beat_at: datetime | None, now: datetime, stale_after: timedelta) -> str:
    if beat_at is None:
        return "missing"
    return "ok" if now - beat_at <= stale_after else "stale"


def worker_status() -> str:
    from app.modules.worker.runner import read_heartbeat

    with get_sessionmaker()() as session:
        beat_at = read_heartbeat(session)
    stale_after = timedelta(seconds=get_settings().worker_stale_after_s)
    return classify_heartbeat(beat_at, SystemClock().now(), stale_after)


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
        return {"status": "degraded", "database": database, "worker": "unknown"}
    try:
        worker = worker_status()
    except Exception:
        logger.exception("healthz: worker heartbeat check failed")
        worker = "unknown"
    return {"status": "ok", "database": database, "worker": worker}
