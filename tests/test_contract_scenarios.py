"""The contract suite's Phase 4 scenario handlers (tests/contract/scenarios.py), run in-process.

The live suite only runs Phase 4 steps once a target's /v1/meta lists phase 4, which ours won't
until reminders exist (#40). These tests run the same handlers now, against this app with the
in-memory doubles and a simulated clock behind /v1/test/clock, so the handlers are known to work
when that happens.
"""

import dataclasses
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.core.config import AppEnv, Settings
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Principal, Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.interface import get_notifier
from app.modules.testing.clock import get_simulated_clock
from tests.contract.fixtures import Scenario, load_fixtures
from tests.contract.scenarios import ScenarioRun
from tests.fakes import ChainLedger, FakeUnitOfWork, RecordingNotifier

FIXTURES = load_fixtures()
SYSTEMS = {name: UUID(s["id"]) for name, s in FIXTURES.systems.items()}


class MovableClock:
    """Real time plus an offset that /v1/test/clock/advance grows, as the service's clock is."""

    def __init__(self) -> None:
        self.offset = 0

    def now(self) -> datetime:
        return datetime.now(UTC) + timedelta(seconds=self.offset)

    def offset_seconds(self) -> int:
        return self.offset

    def advance(self, seconds: int) -> datetime:
        self.offset += seconds
        return self.now()

    def invalidate(self) -> None:  # pragma: no cover - reset isn't called here
        pass


class InProcessApi:
    """The contract suite's Api over TestClient; each key name is its own principal."""

    def __init__(self) -> None:
        self.uow = FakeUnitOfWork()
        for name, system in FIXTURES.systems.items():
            self.uow.seed_system(SYSTEMS[name], name, harm_fields=system.get("harm_fields"))
            for t in system["thresholds"]:
                self.uow.seed_threshold(
                    SYSTEMS[name],
                    t["metric_name"],
                    metric_type=t["metric_type"],
                    direction=t["direction"],
                    limit=t["limit"],
                    valid_from=datetime(2000, 1, 1, tzinfo=UTC),
                )
        principals = {
            "system": Principal(
                actor_id=uuid4(), role=Role.SYSTEM, system_id=SYSTEMS["loan-scorer"]
            ),
            "resume_ranker": Principal(
                actor_id=uuid4(), role=Role.SYSTEM, system_id=SYSTEMS["resume-ranker"]
            ),
            **{r.value: Principal(actor_id=uuid4(), role=r) for r in Role if r is not Role.SYSTEM},
        }
        self.target = SimpleNamespace(keys=dict.fromkeys(principals, "k"), system_id=None)
        self.target.system_id = str(SYSTEMS["loan-scorer"])

        def principal(request: Request) -> Principal:
            key = request.headers.get("authorization", "").removeprefix("Bearer ")
            if key not in principals:
                raise HTTPException(401, "Missing or invalid API key")
            return principals[key]

        clock = self.clock = MovableClock()
        ledger = ChainLedger(self.uow.ledger, clock)
        app = create_app(Settings(enable_test_routes=True, app_env=AppEnv.TEST))
        app.dependency_overrides.update(
            {
                get_principal: principal,
                get_uow: lambda: self.uow,
                get_ledger: lambda: ledger,
                get_notifier: lambda: RecordingNotifier(),
                get_clock: lambda: clock,
                get_simulated_clock: lambda: clock,
            }
        )
        self.client = TestClient(app)

    def call(self, method, path, *, as_, **kwargs):
        headers = {"Authorization": f"Bearer {as_}"} if as_ else {}
        return self.client.request(method, path, headers=headers, **kwargs)

    def wait_for(self, check, what):  # pragma: no cover - no asynchronous steps run here
        result = check()
        assert result, what
        return result


NO_RECEIVER = SimpleNamespace(url=None, bodies=lambda: [])


def run(scenario: Scenario, phases=(3, 4)) -> tuple[ScenarioRun, list[str]]:
    fixtures = dataclasses.replace(FIXTURES, scenarios={**FIXTURES.scenarios, "T": scenario})
    runner = ScenarioRun(InProcessApi(), fixtures, "T", NO_RECEIVER, test_routes=True)
    return runner, runner.run(list(phases))


def test_ex2_not_serious_runs_up_to_phase_4():
    """Model 5 Ex-2 as written: blank rationale 400 (nothing changes), then CLOSED."""
    runner, deferred = run(FIXTURES.scenarios["Ex-2"])
    assert deferred == ["no-report (phase 5)"]
    assert [h["tier"] for h in runner.history] == ["NOT_SERIOUS"]
    assert runner.listed_state() == "CLOSED"


def ex1_steps(*ids: str) -> Scenario:
    ex1 = FIXTURES.scenarios["Ex-1"]
    return dataclasses.replace(ex1, steps=tuple(s for s in ex1.steps if s.id in ids))


def test_ex1_confirm_assign_and_revise_with_deadlines():
    """Model 5 Ex-1 as written: fields confirmed at d0 13:00, OTHER_SERIOUS (deadline A + 15 d),
    then DEATH at d3 superseding it (A + 10 d). AC-08, AC-10, AC-11; the clock travels."""
    runner, _ = run(
        ex1_steps(
            "breach",
            "flagged",
            "flag-audited",
            "confirm-fields",
            "assign-other-serious",
            "revise-to-death",
        )
    )
    assert [(h["tier"], h["previous_tier"]) for h in runner.history] == [
        ("OTHER_SERIOUS", None),
        ("DEATH", "OTHER_SERIOUS"),
    ]
    assert runner.api.clock.offset >= 3 * 86400  # the revision happened on d3
    assert runner.start is not None
    a = datetime.fromisoformat(runner.history[0]["recorded_at"].replace("Z", "+00:00"))
    assert timedelta(hours=4) <= a - runner.start <= timedelta(hours=4, minutes=1)


def test_a_wrong_deadline_fails_the_step():
    ex1 = ex1_steps("breach", "confirm-fields", "assign-other-serious")
    steps = tuple(
        dataclasses.replace(s, expect={**s.expect, "deadline_utc": "P10DT4H"})
        if s.id == "assign-other-serious"
        else s
        for s in ex1.steps
    )
    with pytest.raises(AssertionError, match="assign-other-serious"):
        run(dataclasses.replace(ex1, steps=steps))


def test_ex4_viewer_tier_is_refused():
    """Ex-4 viewer-tier: 403, state unchanged. Its audit expectation (a ledger entry for the
    refused attempt) is not implemented yet, so it's dropped here."""
    ex4 = FIXTURES.scenarios["Ex-4"]
    step = next(s for s in ex4.steps if s.id == "viewer-tier")
    step = dataclasses.replace(
        step, expect={k: v for k, v in step.expect.items() if k != "ledger_records_attempt"}
    )
    runner, _ = run(dataclasses.replace(ex4, steps=(step,)))
    assert runner.history == [] and runner.listed_state() == "FLAGGED"


def test_ex4_refused_attempt_is_not_yet_audited():
    """Documents the open gap: B-14/Ex-4 want refused attempts in the audit trail."""
    ex4 = FIXTURES.scenarios["Ex-4"]
    step = next(s for s in ex4.steps if s.id == "viewer-tier")
    with pytest.raises(AssertionError, match="viewer-tier"):
        run(dataclasses.replace(ex4, steps=(step,)))
