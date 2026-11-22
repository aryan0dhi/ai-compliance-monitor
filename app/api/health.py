"""GET /healthz: liveness, database connectivity and worker heartbeat (Phase 2, Q7.3).

Unauthenticated so the Lightsail container health check can call it. Only an unreachable
database makes it 503: restarting the API container can't fix a stopped worker, so the worker's
state is reported in the body ("ok", "stale" or "missing") for monitoring instead.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import check_database, get_sessionmaker
from app.modules.ledger.models import LedgerCheck
from app.modules.testing.models import ResetCounter

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


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def platform_status(session: Session, now: datetime) -> dict[str, Any]:
    """Contract delta A1: the latest ledger verification run (any trigger) and the number of
    test resets. The age is whole wall-clock seconds, never negative."""
    latest = session.scalars(
        select(LedgerCheck).order_by(LedgerCheck.checked_at.desc(), LedgerCheck.id).limit(1)
    ).first()
    resets = session.scalar(select(ResetCounter.resets).where(ResetCounter.id == 1))
    if latest is None:
        last, age = None, None
    else:
        last = {
            "finished_at": _utc(latest.checked_at),
            "valid": latest.valid,
            "head_check": latest.head_check,
        }
        age = max(0, int((now - latest.checked_at).total_seconds()))
    return {
        "last_verification": last,
        "last_verification_age_seconds": age,
        "reset_count": resets or 0,
    }


def _platform_status() -> dict[str, Any]:
    with get_sessionmaker()() as session:
        return platform_status(session, SystemClock().now())


_UNKNOWN_PLATFORM = {
    "last_verification": None,
    "last_verification_age_seconds": None,
    "reset_count": None,
}


@router.get("/healthz", responses={503: {"description": "Database unreachable"}})
def healthz(response: Response) -> dict[str, Any]:
    """Database, worker and (contract delta A1) version, last ledger verification and reset
    count. Ledger integrity never makes it 503: only an unreachable database does."""
    version = get_settings().app_version
    try:
        check_database()
        database = "ok"
    except Exception:
        logger.exception("healthz: database check failed")
        database = "unavailable"

    if database != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "degraded",
            "database": database,
            "worker": "unknown",
            "version": version,
            **_UNKNOWN_PLATFORM,
        }
    try:
        worker = worker_status()
    except Exception:
        logger.exception("healthz: worker heartbeat check failed")
        worker = "unknown"
    try:
        platform = _platform_status()
    except Exception:
        logger.exception("healthz: verification and reset status failed")
        platform = dict(_UNKNOWN_PLATFORM)
    return {"status": "ok", "database": database, "worker": worker, "version": version, **platform}
