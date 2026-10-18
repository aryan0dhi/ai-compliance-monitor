"""This service against the pod's common interface (docs/pod-interface/openapi.yaml).

- The document is OpenAPI 3.1 and every operation states its phase.
- Every Phase 3 operation it defines exists here.
- Schemathesis generates requests from the POD document (not our own OpenAPI) and validates our
  responses against it, so any drift between this implementation and the agreed interface fails.
  Phase 4/5 operations aren't implemented yet; /v1/test/reset needs PostgreSQL (covered by
  tests/integration and the live contract suite).
"""

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import schemathesis
import yaml
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, reject, settings

from app.api.meta import INTERFACE_VERSION
from app.core.clock import get_clock
from app.core.config import AppEnv, Settings
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.interface import get_notifier
from app.modules.notifications.router import get_webhook_sender
from app.modules.testing.clock import get_simulated_clock
from tests.conftest import T0
from tests.fakes import (
    FakeUnitOfWork,
    FixedClock,
    RecordingLedger,
    RecordingNotifier,
    RecordingSender,
    system_principal,
    user_principal,
)
from tests.test_testing_routes import FakeSimulatedClock

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "pod-interface" / "openapi.yaml"
SPEC = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}
OPERATIONS = [
    (method.upper(), path, op)
    for path, item in SPEC["paths"].items()
    for method, op in item.items()
    if method in HTTP_METHODS
]
SYSTEM_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
VALID_AUTH = "Bearer pod-conformance-key"


def test_document_shape():
    assert SPEC["openapi"].startswith("3.1")
    assert SPEC["info"]["version"] == INTERFACE_VERSION
    assert all(op.get("x-phase") in (3, 4, 5) for _, _, op in OPERATIONS)
    ids = [op["operationId"] for _, _, op in OPERATIONS]
    assert len(ids) == len(set(ids))
    assert "notification" in SPEC["webhooks"]  # outgoing delivery payload is specified


def test_every_phase3_operation_is_implemented():
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    ours = {(m.upper(), p) for p, item in app.openapi()["paths"].items() for m in item}
    missing = [(m, p) for m, p, op in OPERATIONS if op["x-phase"] == 3 and (m, p) not in ours]
    assert missing == []


def test_meta_and_me():
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
    viewer = user_principal(Role.VIEWER)
    app.dependency_overrides[get_principal] = lambda: viewer
    client = TestClient(app)
    assert client.get("/v1/meta").json() == {
        "interface_version": INTERFACE_VERSION,
        "phases": [3],
        "test_routes": True,
    }
    assert client.get("/v1/me").json() == {
        "actor_id": str(viewer.actor_id),
        "role": "viewer",
        "system_id": None,
    }


def test_meta_needs_no_key_and_reports_production():
    app = create_app(Settings(app_env=AppEnv.PRODUCTION, enable_test_routes=True))
    assert TestClient(app).get("/v1/meta").json()["test_routes"] is False


# --- schema-driven conformance ----------------------------------------------------------------


def _app():
    uow = FakeUnitOfWork()
    uow.seed_system(SYSTEM_ID)
    uow.seed_threshold(
        SYSTEM_ID,
        "drift_score",
        metric_type="drift",
        direction="above",
        limit="0.30",
        valid_from=T0 - timedelta(days=1),
    )
    app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))

    def principal(request: Request):
        if request.headers.get("authorization") != VALID_AUTH:
            raise HTTPException(401, "Missing or invalid API key", {"WWW-Authenticate": "Bearer"})
        path = request.url.path
        if path == "/v1/metrics":
            return system_principal(SYSTEM_ID)
        if path == "/v1/webhooks" or path.startswith("/v1/test/"):
            return user_principal(Role.ADMIN)
        return user_principal(Role.VIEWER)

    clock = FakeSimulatedClock()
    app.dependency_overrides.update(
        {
            get_principal: principal,
            get_uow: lambda: uow,
            get_ledger: lambda: RecordingLedger(),
            get_notifier: lambda: RecordingNotifier(),
            get_clock: lambda: FixedClock(T0),
            get_webhook_sender: lambda: RecordingSender(),
            get_simulated_clock: lambda: clock,
        }
    )
    return app


_loaded = schemathesis.openapi.from_dict(SPEC)
_loaded.app = _app()
pod_schema = _loaded.exclude(path_regex="/disposition$|/report|^/v1/test/reset$")


@pod_schema.auth()
class ConformanceKey:
    def get(self, case, ctx):
        return VALID_AUTH

    def set(self, case, data, ctx):
        case.headers = {**(case.headers or {}), "Authorization": data}


@pytest.mark.fuzz
@pod_schema.parametrize()
@settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
def test_conforms_to_pod_interface(case):
    # Same documented exclusion as tests/test_fuzz.py: date-times that are schema-valid but fall
    # outside year 1-9999 once converted to UTC are an intentional 400.
    if isinstance(case.body, dict) and isinstance(case.body.get("timestamp"), str):
        try:
            datetime.fromisoformat(case.body["timestamp"]).astimezone(UTC)
        except (ValueError, OverflowError):
            reject()
    case.call_and_validate()
