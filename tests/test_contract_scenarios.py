"""The contract suite's Phase 4 scenario handlers (tests/contract/scenarios.py), run in-process.

The live suite only runs Phase 4 steps once a target's /v1/meta lists phase 4. These tests run
the same handlers now, against this app with the in-memory doubles and a simulated clock behind
/v1/test/clock, so the handlers are known to work when it does. A stand-in receiver runs the
worker's reminder sweep and dispatch whenever the suite looks at its webhook bodies.
"""

import dataclasses
import json
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
from app.modules.notifications.dispatcher import dispatch_due
from app.modules.notifications.interface import get_notifier
from app.modules.review.reminders import sweep_reminders
from app.modules.testing.clock import get_simulated_clock
from tests.contract.fixtures import Scenario, load_fixtures
from tests.contract.scenarios import ScenarioRun
from tests.fakes import ChainLedger, FakeUnitOfWork, RecordingNotifier, RecordingSender

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
        ledger = self.ledger = ChainLedger(self.uow.ledger, clock)
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


class WorkerReceiver:
    """A reminder webhook on the in-memory app: reading the bodies runs one worker pass."""

    url = "https://receiver.example/hook"

    def __init__(self, api: InProcessApi) -> None:
        self.api, self.sender = api, RecordingSender()
        api.uow.seed_webhook(self.url, event_types=("reminder",))

    def bodies(self) -> list[dict]:
        api = self.api
        sweep_reminders(lambda: api.uow, clock=api.clock, max_delay=timedelta(minutes=15))
        dispatch_due(
            lambda: api.uow, sender=self.sender, ledger=api.ledger, clock=api.clock, max_attempts=3
        )
        return [json.loads(c["body"]) for c in self.sender.calls]


NO_RECEIVER = SimpleNamespace(url=None, bodies=lambda: [])


def run(scenario: Scenario, phases=(3, 4), *, webhooks=False) -> tuple[ScenarioRun, list[str]]:
    fixtures = dataclasses.replace(FIXTURES, scenarios={**FIXTURES.scenarios, "T": scenario})
    api = InProcessApi()
    receiver = WorkerReceiver(api) if webhooks else NO_RECEIVER
    runner = ScenarioRun(api, fixtures, "T", receiver, test_routes=True)
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


def test_ex1_reminders_at_50_80_and_100_percent():
    """Model 5 Ex-1 through to d10 13:00: three reminders, one per checkpoint (AC-12)."""
    runner, _ = run(
        ex1_steps(
            "breach",
            "confirm-fields",
            "assign-other-serious",
            "revise-to-death",
            "reminders",
        ),
        webhooks=True,
    )
    bodies = runner.receiver.bodies()
    assert sorted(b["reminder"]["elapsed_percent"] for b in bodies) == [50, 80, 100]
    assert len({b["notification_id"] for b in bodies}) == 3  # later passes add nothing


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


def ledger_types(runner) -> list[str]:
    return [e.entry_type for e in runner.api.uow.ledger.for_event(UUID(runner.event_id))]


def test_ex4_viewer_tier_is_refused_and_audited():
    """Ex-4 viewer-tier as written: 403, state unchanged, and one audit entry for the attempt,
    attributed to the viewer (D-26)."""
    ex4 = FIXTURES.scenarios["Ex-4"]
    step = next(s for s in ex4.steps if s.id == "viewer-tier")
    runner, _ = run(dataclasses.replace(ex4, steps=(step,)))
    assert runner.history == [] and runner.listed_state() == "FLAGGED"
    assert ledger_types(runner)[-1] == "REFUSED"


def test_ex4_runs_up_to_phase_4():
    """Model 5 Ex-4 up to phase 4, except premature-report's UNDER_REVIEW: that needs a webhook
    to accept the flag notification (D-21), and this in-process run has none."""
    ex4 = FIXTURES.scenarios["Ex-4"]
    steps = tuple(
        dataclasses.replace(s, expect={k: v for k, v in s.expect.items() if k != "state"})
        if s.id == "premature-report"
        else s
        for s in ex4.steps
    )
    runner, deferred = run(dataclasses.replace(ex4, steps=steps))
    assert deferred == []
    assert ledger_types(runner).count("REFUSED") == 2


def test_ex1_report_of_the_revised_determination():
    """Model 5 Ex-1 report at d10 13:00 (B-12, AC-13): the handler checks the DEATH
    determination, the three times and that a second call returns the same document."""
    runner, deferred = run(
        ex1_steps("breach", "confirm-fields", "assign-other-serious", "revise-to-death", "report"),
        phases=(3, 4, 5),
    )
    assert deferred == []
    assert [r.disposition_seq for r in runner.api.uow.reports.rows.values()] == [2]


def test_ex2_runs_to_phase_5_without_a_report():
    """Model 5 Ex-2 no-report: the NOT_SERIOUS event's report is 409."""
    runner, deferred = run(FIXTURES.scenarios["Ex-2"], phases=(3, 4, 5))
    assert deferred == []
    assert runner.api.uow.reports.rows == {}


def test_ex4_premature_report_is_refused_and_audited():
    """Ex-4 premature-report: 409 before any disposition, with the attempt in the audit trail.
    Its UNDER_REVIEW expectation is dropped (no webhook here, D-21)."""
    ex4 = FIXTURES.scenarios["Ex-4"]
    step = next(s for s in ex4.steps if s.id == "premature-report")
    expect = {k: v for k, v in step.expect.items() if k != "state"}
    runner, _ = run(dataclasses.replace(ex4, steps=(dataclasses.replace(step, expect=expect),)))
    assert runner.api.uow.reports.rows == {}
    assert ledger_types(runner)[-1] == "REFUSED"


def test_a_wrong_report_tier_fails_the_step():
    """The report handler compares the determination with the latest disposition."""
    runner, _ = run(ex1_steps("breach", "confirm-fields", "assign-other-serious"))
    runner.history[-1] = {**runner.history[-1], "tier": "DEATH"}
    report = next(s for s in FIXTURES.scenarios["Ex-1"].steps if s.id == "report")
    with pytest.raises(AssertionError):
        runner.do_get_report(report)
