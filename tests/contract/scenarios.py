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
    {"await_reminders", "get_report", "await_gap"} | {"tamper_export", "await_alert"}
)
# A breach is FLAGGED until a webhook accepts its flag notification, then UNDER_REVIEW (D-21). With
# webhooks registered that can happen within one worker pass, so a step that reads the state right
# after the breach accepts either.
SAME_AS = {"FLAGGED": ("FLAGGED", "UNDER_REVIEW")}


def state_matches(want: str, got: str) -> bool:
    return got in SAME_AS.get(want, (want,))


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
        # confirm_fields steps collect here; the next disposition sends them (B-08).
        self.confirmations: dict[str, dict[str, str]] = {}
        self.history: list[dict] = []

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

    def listed_state(self) -> str:
        """The event's state from the listing, which adds no audit entry (D-08)."""
        page = self.api.call(
            "GET", "/v1/events", as_="viewer", params={"system_id": self.system_id, "limit": 200}
        )
        assert page.status_code == 200, page.text
        return next(e["state"] for e in page.json()["items"] if e["event_id"] == self.event_id)

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
        for field in ("type", "metric_name"):
            if field in want:
                assert event[field] == want[field], (field, event[field])
        if "state" in want:
            assert state_matches(want["state"], event["state"]), event["state"]
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
        state = self.listed_state()
        assert state_matches(step.expect["state"], state), state

    def do_confirm_fields(self, step: Step) -> None:
        """REQ-07: the reviewer's confirmations travel in the next disposition body; the
        interface has no separate call for them (DispositionIn.harm_fields)."""
        self.key(step.actor)
        for status, names in (
            ("CONFIRMED", step.args.get("confirmed", [])),
            ("NOT_APPLICABLE", step.args.get("not_applicable", [])),
        ):
            self.confirmations.update({name: {"status": status} for name in names})

    def do_disposition(self, step: Step) -> None:
        """B-08/B-09: assign or revise a tier; a refused call changes nothing."""
        key, want = self.key(step.actor), step.expect
        body: dict = {"tier": step.args["tier"]}
        if "rationale" in step.args:
            body["rationale"] = step.args["rationale"]
        if self.confirmations:
            body["harm_fields"] = self.confirmations
        state_before = self.listed_state()
        audit_before = self.audit() if "ledger_records_attempt" in want else None
        resp = self.api.call("POST", f"/v1/events/{self.event_id}/disposition", as_=key, json=body)
        assert resp.status_code == want.get("status", 200), resp.text
        if audit_before is not None:
            new = self.audit()[len(audit_before) :]
            assert len(new) == 1 and new[0]["actor_id"] == self.actor_id(key), new
        if resp.status_code >= 400:
            assert_error_body(resp)
            assert self.listed_state() == state_before
            return
        self.confirmations = {}
        event = resp.json()
        history = event["revision_history"]
        # B-09: earlier dispositions are kept as they were; this one is appended.
        assert history[: len(self.history)] == self.history, history
        assert len(history) == len(self.history) + 1, history
        assert history[-1]["tier"] == body["tier"], history
        assert history[-1]["reviewer_id"] == self.actor_id(key), history
        assert history[-1]["previous_tier"] == (self.history[-1]["tier"] if self.history else None)
        self.history = history
        serious = body["tier"] != "NOT_SERIOUS"
        assert event["state"] == want.get("state", "DISPOSITIONED" if serious else "CLOSED")
        if "deadline_utc" in want:
            if want["deadline_utc"] is not None:
                pytest.fail("Article 73 deadline checks (B-10) arrive with issue #39")
            assert event["deadline_utc"] is None, event["deadline_utc"]
        if "dispositions" in want:
            assert len(history) == want["dispositions"], history
        if want.get("second_supersedes_first"):
            assert history[1]["previous_tier"] == history[0]["tier"], history
        if "escalations" in want and self.receiver.url is not None:
            reminders = [
                b
                for b in self.receiver.bodies()
                if b.get("notification_type") == "reminder"
                and self.event_id in (b.get("event_id"), b.get("event", {}).get("event_id"))
            ]
            assert len(reminders) == want["escalations"], reminders


HANDLED_ACTIONS = frozenset(
    name.removeprefix("do_") for name in dir(ScenarioRun) if name.startswith("do_")
)
KNOWN_ACTIONS = HANDLED_ACTIONS | DEFERRED_ACTIONS
