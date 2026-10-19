"""POST /test/setup and GET /test/report: the system side of the course cross-team testing
interface (docs/testing-interface/system-openapi.yaml, D-19).

Mounted at the service root (not /v1) only when settings.test_routes_enabled, so they return 404
in production. Unauthenticated, because the tester calls them knowing only the system's base
URL; that is acceptable only because setup touches nothing but the test environment.

Errors are {"message", "source"} as the interface requires, not FastAPI's {"detail"}, so the body
is parsed here instead of by a Pydantic model.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.db.uow import UnitOfWork, get_uow
from app.modules.testing.clock import SimulatedClock, get_simulated_clock
from app.modules.testing.service import reset_test_environment
from app.modules.testing.sessions import (
    SessionExists,
    SessionNotReady,
    SetupInProgress,
    TestSessions,
    UnknownSession,
    get_test_sessions,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/test", tags=["testing interface"])

MAX_SESSION_ID_LENGTH = 200

_ERROR = {
    "type": "object",
    "required": ["message", "source"],
    "properties": {
        "message": {"type": "string", "minLength": 1},
        "source": {"enum": ["tester", "system", "communication", "unknown"]},
    },
}


def _error_response(description: str) -> dict[str, Any]:
    return {"description": description, "content": {"application/json": {"schema": _ERROR}}}


def error(status_code: int, message: str, source: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"message": message, "source": source})


def _parse_setup(raw: bytes) -> tuple[str, dict[str, Any]] | str:
    """(session_id, setup) from the request body, or the reason it is invalid."""
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "body must be JSON"
    if not isinstance(body, dict):
        return "body must be a JSON object"
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return "session_id must be a non-empty string"
    if len(session_id) > MAX_SESSION_ID_LENGTH:
        return f"session_id must be at most {MAX_SESSION_ID_LENGTH} characters"
    setup = body.get("setup", {})
    if not isinstance(setup, dict):
        return "setup must be a JSON object"
    return session_id, setup


@router.post(
    "/setup",
    status_code=status.HTTP_201_CREATED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["session_id"],
                        "properties": {
                            "session_id": {"type": "string", "minLength": 1},
                            "setup": {"type": "object"},
                        },
                    }
                }
            },
        }
    },
    responses={
        201: {"description": "Session ready: baseline fixtures loaded, clock at real time"},
        400: _error_response("Invalid request"),
        409: _error_response("Session id already used, or another setup is in progress"),
        500: _error_response("Preparing the system failed"),
    },
)
async def setup_session(
    request: Request,
    sessions: TestSessions = Depends(get_test_sessions),
    uow: UnitOfWork = Depends(get_uow),
    clock: SimulatedClock = Depends(get_simulated_clock),
) -> JSONResponse:
    parsed = _parse_setup(await request.body())
    if isinstance(parsed, str):
        return error(status.HTTP_400_BAD_REQUEST, parsed, "tester")
    session_id, setup = parsed
    try:
        sessions.begin(session_id, setup)
    except SessionExists:
        return error(status.HTTP_409_CONFLICT, "session_id already exists", "tester")
    except SetupInProgress as exc:
        return error(status.HTTP_409_CONFLICT, str(exc), "tester")
    try:
        await run_in_threadpool(reset_test_environment, uow)
        clock.invalidate()
    except Exception:
        sessions.abandon(session_id)
        logger.exception("test session setup failed")
        return error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "could not reset the test environment (is the database reachable?)",
            "system",
        )
    sessions.activate(session_id)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content={"session_id": session_id})


@router.get(
    "/report",
    responses={
        200: {"description": "Code coverage of the requests made since this session's setup"},
        400: _error_response("Missing session_id"),
        404: _error_response("Unknown session"),
        409: _error_response("Setup still in progress"),
    },
)
def session_report(
    session_id: str | None = None,
    sessions: TestSessions = Depends(get_test_sessions),
) -> JSONResponse:
    if not session_id:
        return error(status.HTTP_400_BAD_REQUEST, "the session_id query is required", "tester")
    try:
        code = sessions.coverage(session_id)
    except UnknownSession:
        return error(status.HTTP_404_NOT_FOUND, "unknown session_id", "tester")
    except SessionNotReady:
        return error(status.HTTP_409_CONFLICT, "setup is still in progress", "tester")
    return JSONResponse(
        content={
            "session_id": session_id,
            "coverage": {
                "code": {"covered": code.covered, "total": code.total, "basis": code.basis}
            },
        }
    )
