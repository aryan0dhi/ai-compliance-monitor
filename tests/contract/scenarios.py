"""Runs the Model 5 scenarios of the fixtures file (docs/pod-interface/fixtures.json).

A scenario is data: steps with a phase, an actor, an action and what to expect. A target runs the
steps up to the phases it provides (GET /v1/meta); later steps are reported as deferred, never
skipped. Supporting Phase 4 means adding handlers here, not changing the tests.
"""

import time
from datetime import UTC, datetime, timedelta

import pytest

from tests.contract.fixtures import Fixtures, Scenario, Step, parse_duration

# Which configured key acts as each fixture system.
SYSTEM_KEYS = {"loan-scorer": "system", "resume-ranker": "resume_ranker"}
# Actions in the fixtures file that no handler runs yet (Phase 4/5, or covered by other tests).
DEFERRED_ACTIONS = frozenset(
    {"confirm_fields", "disposition", "await_reminders", "get_report", "await_gap"}
    | {"tamper_export", "await_alert"}
)
# detected_at may differ from the service clock reads around the request by this much.
CLOCK_TOLERANCE = timedelta(seconds=1)

_SEQ = iter(range(1, 10**9))


def sample_time() -> str:
    # Unique per call, so repeats never collide on (system_id, metric_name, timestamp).
    return (datetime.now(UTC) + timedelta(microseconds=next(_SEQ))).isoformat()


def parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def assert_error_body(resp) -> None:
    """B-22: every error is JSON {"detail": <str> or a list of {loc, msg, type}}."""
    assert resp.headers.get("content-type", "").startswith("application/json"), resp.text
    body = resp.json()
    assert isinstance(body, dict) and set(body) == {"detail"}, body
    detail = body["detail"]
    assert isinstance(detail, str) or (
        isinstance(detail, list)
        and all(isinstance(e, dict) and {"loc", "msg", "type"} <= set(e) for e in detail)
    ), body


class ScenarioRun:
    def __init__(self, api, fixtures: Fixtures, name: str, receiver, test_routes: bool) -> None:
        self.api, self.fixtures = api, fixtures
        self.receiver, self.test_routes = receiver, test_routes
        self.scenario: Scenario = fixtures.scenarios[name]
        self.system = self.scenario.system
        self.event_id: str | None = None
        self.window: tuple[datetime, datetime] | None = None
        self.submitted_at = 0.0
        self.actors: dict[str, str] = {}

    @property
    def system_id(self) -> str:
        if SYSTEM_KEYS[self.system] == "system":
            return self.api.target.system_id
        return self.fixtures.systems[self.system]["id"]

    def key(self, actor: str) -> str:
        key = SYSTEM_KEYS[self.system] if actor == "system" else actor
        if key not in self.api.target.keys:
            pytest.fail(f"CONTRACT_{key.upper()}_KEY is required for Model 5 {self.scenario.name}")
        return key

    def service_now(self) -> datetime:
        if not self.test_routes:
            return datetime.now(UTC)
        resp = self.api.call("GET", "/v1/test/clock", as_="admin")
        assert resp.status_code == 200, resp.text
        return parse(resp.json()["simulated_time"])

    def actor_id(self, key: str) -> str:
        if key not in self.actors:
            self.actors[key] = self.api.call("GET", "/v1/me", as_=key).json()["actor_id"]
        return self.actors[key]

    def audit(self) -> list[dict]:
        resp = self.api.call("GET", f"/v1/events/{self.event_id}/audit", as_="viewer")
        assert resp.status_code == 200, resp.text
        return resp.json()

    def run(self, phases: list[int]) -> list[str]:
        """Run the steps this target can; returns the deferred ones as "id (phase n)"."""
        todo, deferred = self.scenario.split(phases, webhooks=self.receiver.url is not None)
        assert todo, f"{self.scenario.name}: no step applies to phases {phases}"
        if setup := self.scenario.setup:
            other = self.fixtures.scenarios[setup["scenario"]]
            todo = [s for s in other.steps if s.id in setup["steps"]] + todo
        for step in todo:
            try:
                getattr(self, f"do_{step.action}")(step)
            except AssertionError as exc:
                raise AssertionError(f"{self.scenario.name} step {step.id}: {exc}") from exc
        return [f"{s.id} (phase {s.phase})" for s in deferred]

    def do_submit_metric(self, step: Step) -> None:
        metric = step.args["metric"]
        body = {
            "system_id": self.system_id,
            "metric_name": metric,
            "metric_type": self.fixtures.threshold(self.system, metric=metric)["metric_type"],
            "value": step.args["value"],
            "timestamp": sample_time(),
        }
        before = self.service_now()
        resp = self.api.call("POST", "/v1/metrics", as_=self.key(step.actor), json=body)
        self.window = (before, self.service_now())
        self.submitted_at = time.monotonic()
        assert resp.status_code == step.expect["status"], resp.text
        if step.expect.get("flagged"):
            self.event_id = resp.json().get("flagged_event_id")
            assert self.event_id, resp.text

    def do_get_event(self, step: Step) -> None:
        key, want = self.key(step.actor), step.expect
        before = self.audit() if "audit_adds" in want else []
        resp = self.api.call("GET", f"/v1/events/{self.event_id}", as_=key)
        assert resp.status_code == want.get("status", 200), resp.text
        event = resp.json()
        for field in ("type", "state", "metric_name"):
            if field in want:
                assert event[field] == want[field], (field, event[field])
        if "observed_value" in want:
            assert event["observed_value"] == pytest.approx(want["observed_value"])
        if want.get("threshold_value") == "fixture":
            t = self.fixtures.threshold(self.system, metric=event["metric_name"])
            assert event["threshold_value"] == pytest.approx(float(t["limit"]))
        if want.get("detected_in_call"):
            assert self.window is not None
            low, high = self.window
            detected = parse(event["detected_at"])
            assert low - CLOCK_TOLERANCE <= detected <= high + CLOCK_TOLERANCE, detected
        if "audit_adds" in want:
            new, adds = self.audit()[len(before) :], want["audit_adds"]
            assert [a["action"] for a in new] == [adds["action"]] * adds["count"], new
            assert all(a["actor_id"] == self.actor_id(key) for a in new), new

    def do_get_audit(self, step: Step) -> None:
        actions = {a["action"] for a in self.audit()}
        assert set(step.expect["actions_include"]) <= actions, actions

    def do_await_webhook(self, step: Step) -> None:
        def delivered():
            return next(
                (
                    b
                    for b in self.receiver.bodies()
                    if self.event_id in (b.get("event_id"), b.get("event", {}).get("event_id"))
                ),
                None,
            )

        body = self.api.wait_for(delivered, f"a webhook notification for {self.event_id}")
        limit = parse_duration(step.expect["within"]).total_seconds()
        assert time.monotonic() - self.submitted_at <= limit
        assert (
            body.get("notification_type", "flag_notification") == step.expect["notification_type"]
        )

    def do_attempt(self, step: Step) -> None:
        """Calls the actor may not make: each is refused; the event and its audit don't change."""
        key, before = self.key(step.actor), self.audit()
        threshold = self.fixtures.systems[self.system]["thresholds"][0]
        bodies = {
            "/v1/webhooks": {
                "url": "https://example.com/hook",
                "event_types": ["flag_notification"],
            },
            "/v1/metrics": {
                "system_id": self.system_id,
                "metric_name": threshold["metric_name"],
                "metric_type": threshold["metric_type"],
                "value": 0.5,
                "timestamp": sample_time(),
            },
        }
        for call in step.args["calls"]:
            method, path = call.split(" ")
            resp = self.api.call(method, path, as_=key, json=bodies[path])
            assert resp.status_code == step.expect["status"], (call, resp.text)
            assert_error_body(resp)
        assert len(self.audit()) == len(before) + step.expect["audit_adds"]["count"]
        page = self.api.call(
            "GET", "/v1/events", as_="viewer", params={"system_id": self.system_id, "limit": 200}
        )
        state = next(e["state"] for e in page.json()["items"] if e["event_id"] == self.event_id)
        assert state == step.expect["state"], state


HANDLED_ACTIONS = frozenset(
    name.removeprefix("do_") for name in dir(ScenarioRun) if name.startswith("do_")
)
KNOWN_ACTIONS = HANDLED_ACTIONS | DEFERRED_ACTIONS
