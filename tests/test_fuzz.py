"""Schema-driven fuzzing of the /v1 API with Schemathesis (Phase 2, Q7.4).

Generates requests from the OpenAPI document and checks that none causes a server error and
that responses match the documented schemas. Auth, ledger, notifier, unit of work and clock are
the explicit test doubles, so requests reach the real validation and business logic instead of
stopping at the fail-closed 401.

Marked `fuzz` (slower). Run: pytest -m fuzz  (deeper: FUZZ_EXAMPLES=1000 pytest -m fuzz)
"""

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import schemathesis
from fastapi import Request
from hypothesis import HealthCheck, reject, settings

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.interface import get_notifier
from tests.conftest import T0
from tests.fakes import (
    FakeUnitOfWork,
    FixedClock,
    RecordingLedger,
    RecordingNotifier,
    system_principal,
    user_principal,
)

pytestmark = pytest.mark.fuzz

SYSTEM_ID = UUID("550e8400-e29b-41d4-a716-446655440000")


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
    app = create_app()

    def principal(request: Request):
        # A system key for writes and a viewer for reads, so both paths are exercised.
        if request.method == "POST":
            return system_principal(SYSTEM_ID)
        return user_principal(Role.VIEWER)

    app.dependency_overrides.update(
        {
            get_principal: principal,
            get_uow: lambda: uow,
            get_ledger: lambda: RecordingLedger(),
            get_notifier: lambda: RecordingNotifier(),
            get_clock: lambda: FixedClock(T0),
        }
    )
    return app


APP = _app()
# /healthz depends on a live database, which unit runs don't have; fuzz only the /v1 contract.
schema = schemathesis.openapi.from_asgi("/openapi.json", APP).include(path_regex="^/v1/")


@schema.parametrize()
@settings(
    max_examples=int(os.environ.get("FUZZ_EXAMPLES", "60")),
    deadline=None,
    suppress_health_check=list(HealthCheck),
)
def test_api_never_errors_and_matches_schema(case):
    # Known, intentional 400: a date-time that is schema-valid but falls outside Python's
    # datetime range once converted to UTC (e.g. 0001-01-01T00:00:01+00:01 is year 0 in UTC).
    # OpenAPI has no standard way to express a date range, so skip exactly those inputs.
    if isinstance(case.body, dict) and isinstance(case.body.get("timestamp"), str):
        try:
            datetime.fromisoformat(case.body["timestamp"]).astimezone(UTC)
        except (ValueError, OverflowError):
            reject()
    case.call_and_validate()
