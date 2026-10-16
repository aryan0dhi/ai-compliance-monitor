"""HTTP contract tests for /v1/metrics and /v1/events (Phase 2, Section 5).

Test doubles are installed with FastAPI `dependency_overrides` on a per-test app. Production
wiring is never modified; the `TestProductionWiring` class checks that without overrides
everything stays fail-closed.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.clock import get_clock
from app.db.uow import get_uow
from app.main import create_app
from app.modules.auth.interface import Role, get_principal
from app.modules.ledger.interface import get_ledger
from app.modules.notifications.interface import get_notifier
from tests.conftest import T0
from tests.fakes import system_principal, user_principal

# Section 5.2 examples.
SYSTEM_ID = UUID("550e8400-e29b-41d4-a716-446655440000")
VALID_METRIC = {
    "system_id": str(SYSTEM_ID),
    "metric_name": "drift_score",
    "metric_type": "drift",
    "value": 0.35,
    "timestamp": "2026-09-27T20:00:00Z",
}


class Api:
    def __init__(self, uow, ledger, notifier, clock):
        self.app = create_app()
        self.principal = system_principal(SYSTEM_ID)
        self.app.dependency_overrides.update(
            {
                get_principal: lambda: self.principal,
                get_uow: lambda: uow,
                get_ledger: lambda: ledger,
                get_notifier: lambda: notifier,
                get_clock: lambda: clock,
            }
        )
        self.client = TestClient(self.app)

    def as_user(self, role: Role) -> "Api":
        self.principal = user_principal(role)
        return self


@pytest.fixture
def api(uow, ledger, notifier, clock) -> Api:
    uow.seed_system(SYSTEM_ID)
    uow.seed_threshold(
        SYSTEM_ID,
        "drift_score",
        metric_type="drift",
        direction="above",
        limit="0.30",
        valid_from=T0 - timedelta(days=1),
    )
    return Api(uow, ledger, notifier, clock)


class TestPostMetrics:
    def test_section_5_2_example_breach(self, api, uow):
        resp = api.client.post("/v1/metrics", json=VALID_METRIC)
        assert resp.status_code == 201
        body = resp.json()
        event_id = UUID(body.pop("flagged_event_id"))
        assert body == VALID_METRIC
        assert uow.events.get(event_id).state == "FLAGGED"

    def test_no_breach_omits_flagged_event_id(self, api):
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "value": 0.10})
        assert resp.status_code == 201
        assert "flagged_event_id" not in resp.json()

    def test_value_exactly_at_threshold_does_not_flag(self, api, uow):
        # AC-03 boundary through the HTTP layer: JSON 0.3 must be compared exactly with 0.30.
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "value": 0.3})
        assert resp.status_code == 201
        assert "flagged_event_id" not in resp.json()
        assert uow.events.rows == {}

    def test_integer_value_accepted(self, api):
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "value": 1})
        assert resp.status_code == 201 and resp.json()["value"] == 1

    def test_timestamp_with_offset_normalized_to_utc(self, api):
        resp = api.client.post(
            "/v1/metrics", json={**VALID_METRIC, "timestamp": "2026-09-27T16:00:00-04:00"}
        )
        assert resp.status_code == 201
        assert resp.json()["timestamp"] == "2026-09-27T20:00:00Z"

    @pytest.mark.parametrize(
        "change",
        [
            {"system_id": None},
            {"metric_name": None},
            {"metric_type": None},
            {"value": None},
            {"timestamp": None},
            {"system_id": "not-a-uuid"},
            {"metric_name": ""},
            {"metric_name": "   "},
            {"metric_type": "latency"},
            {"value": "0.35"},
            {"value": True},
            {"timestamp": "2026-09-27T20:00:00"},
            {"timestamp": "yesterday"},
            # Regressions found by edge-case probing (review, 2026-10-16):
            {"value": 10**400},  # overflowed to null in the response
            {"metric_name": "drift_score "},  # would never match its threshold
            {"metric_name": " drift_score"},
            {"metric_name": "a\u0000b"},  # PostgreSQL text rejects NUL -> 500
            {"metric_name": "a\nb"},
            {"timestamp": "0001-01-01T00:00:00+01:00"},  # UTC conversion overflow -> 500
            {"timestamp": "9999-12-31T23:30:00-01:00"},
            {"timestamp": 0},  # numbers were read as Unix time (found by Schemathesis)
            {"timestamp": "1695844800"},
            {"metric_name": "a\u0085b"},  # C1 control character
        ],
        ids=lambda c: f"{next(iter(c))}={str(next(iter(c.values())))[:20]!r}",
    )
    def test_invalid_body_is_400(self, api, uow, change):
        body = {**VALID_METRIC, **change}
        body = {k: v for k, v in body.items() if v is not None}
        resp = api.client.post("/v1/metrics", json=body)
        assert resp.status_code == 400, resp.json()
        assert uow.metrics.rows == {}

    @pytest.mark.parametrize("raw", ["{not json", '{"value": NaN}', "[]"])
    def test_malformed_json_is_400(self, api, raw):
        resp = api.client.post(
            "/v1/metrics", content=raw, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400

    def test_lone_surrogate_in_metric_name_is_400(self, api):
        # Escaped in JSON (as a real client would send it); not encodable as UTF-8 for PostgreSQL.
        raw = (
            f'{{"system_id": "{SYSTEM_ID}", "metric_name": "a\\ud8a1b", '
            '"metric_type": "drift", "value": 0.1, "timestamp": "2026-09-27T20:00:00Z"}'
        )
        resp = api.client.post(
            "/v1/metrics", content=raw, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400

    def test_nan_value_is_400(self, api):
        # Python's json module (used by FastAPI) accepts the non-standard NaN literal.
        raw = (
            f'{{"system_id": "{SYSTEM_ID}", "metric_name": "drift_score", '
            '"metric_type": "drift", "value": NaN, "timestamp": "2026-09-27T20:00:00Z"}'
        )
        resp = api.client.post(
            "/v1/metrics", content=raw, headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 400

    def test_key_scoped_to_other_system_is_403(self, api, uow):
        api.principal = system_principal(uuid4())
        resp = api.client.post("/v1/metrics", json=VALID_METRIC)
        assert resp.status_code == 403
        assert uow.metrics.rows == {}

    def test_human_key_is_403(self, api):
        resp = api.as_user(Role.ADMIN).client.post("/v1/metrics", json=VALID_METRIC)
        assert resp.status_code == 403

    def test_unknown_system_is_400(self, api):
        other = uuid4()
        api.principal = system_principal(other)
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "system_id": str(other)})
        assert resp.status_code == 400

    def test_metric_type_mismatch_is_400(self, api):
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "metric_type": "performance"})
        assert resp.status_code == 400

    def test_identical_repeat_is_200_temporary_d04(self, api, uow):
        first = api.client.post("/v1/metrics", json=VALID_METRIC)
        again = api.client.post("/v1/metrics", json=VALID_METRIC)
        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json() == first.json()
        assert len(uow.events.rows) == 1

    def test_conflicting_repeat_is_409_temporary_d04(self, api):
        api.client.post("/v1/metrics", json=VALID_METRIC)
        resp = api.client.post("/v1/metrics", json={**VALID_METRIC, "value": 0.36})
        assert resp.status_code == 409


class TestGetEvents:
    @pytest.fixture
    def flagged(self, api) -> str:
        resp = api.client.post("/v1/metrics", json=VALID_METRIC)
        api.as_user(Role.VIEWER)
        return resp.json()["flagged_event_id"]

    def test_list_envelope_and_summary(self, api, flagged, clock):
        resp = api.client.get("/v1/events")
        assert resp.status_code == 200
        assert resp.json() == {
            "items": [
                {
                    "event_id": flagged,
                    "system_id": str(SYSTEM_ID),
                    "type": "threshold_breach",
                    "state": "FLAGGED",
                    "detected_at": "2026-09-27T20:01:00Z",
                    "metric_name": "drift_score",
                }
            ],
            "total": 1,
            "limit": 50,
            "offset": 0,
        }

    @pytest.mark.parametrize(
        ("query", "count"),
        [
            (f"system_id={SYSTEM_ID}", 1),
            (f"system_id={uuid4()}", 0),
            ("state=FLAGGED", 1),
            ("state=CLOSED", 0),
            ("type=threshold_breach", 1),
            ("type=monitoring_gap", 0),
            ("since=2026-09-27T20:01:00Z", 1),
            ("since=2026-09-27T20:02:00Z", 0),
            ("limit=1&offset=0", 1),
            ("limit=1&offset=1", 0),
        ],
    )
    def test_filters(self, api, flagged, query, count):
        resp = api.client.get(f"/v1/events?{query}")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == count

    @pytest.mark.parametrize(
        "query",
        [
            "state=flagged",
            "type=BREACH",
            "system_id=abc",
            "since=2026-09-27T20:00:00",
            "limit=0",
            "limit=201",
            "offset=-1",
            "since=0.0",  # was read as Unix time 1970-01-01 (found by Schemathesis)
            "since=1695844800",
        ],
    )
    def test_invalid_query_is_400(self, api, flagged, query):
        assert api.client.get(f"/v1/events?{query}").status_code == 400

    def test_detail(self, api, flagged, ledger):
        resp = api.client.get(f"/v1/events/{flagged}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_id"] == flagged
        assert body["state"] == "FLAGGED"
        assert body["metric_name"] == "drift_score"
        assert body["metric_timestamp"] == "2026-09-27T20:00:00Z"
        assert body["observed_value"] == 0.35
        assert body["threshold_value"] == 0.30
        assert body["detected_at"] == "2026-09-27T20:01:00Z"
        assert body["retain_until"] == "2027-03-27T20:01:00Z"
        # Phase 4 fields: present per Section 5.2, not yet populated.
        assert body["deadline_utc"] is None
        assert body["harm_fields"] is None
        assert body["revision_history"] == []
        assert ledger.calls[-1]["entry_type"] == "VIEW"

    def test_unknown_event_is_404(self, api, flagged):
        assert api.client.get(f"/v1/events/{uuid4()}").status_code == 404

    def test_malformed_event_id_is_404(self, api, flagged):
        assert api.client.get("/v1/events/not-a-uuid").status_code == 404

    def test_system_key_cannot_read_events(self, api, flagged):
        api.principal = system_principal(SYSTEM_ID)
        assert api.client.get("/v1/events").status_code == 403
        assert api.client.get(f"/v1/events/{flagged}").status_code == 403


class TestProductionWiring:
    """No test doubles for auth: every endpoint must be denied (fail closed)."""

    @pytest.fixture
    def prod_client(self) -> TestClient:
        return TestClient(create_app())

    @pytest.mark.parametrize(
        ("method", "path", "kwargs"),
        [
            ("post", "/v1/metrics", {"json": VALID_METRIC}),
            ("post", "/v1/metrics", {"json": {"garbage": True}}),  # 401 before body validation
            ("get", "/v1/events", {}),
            ("get", f"/v1/events/{uuid4()}", {}),
        ],
    )
    def test_unauthenticated_requests_denied(self, prod_client, method, path, kwargs):
        resp = getattr(prod_client, method)(
            path, headers={"Authorization": "Bearer anything"}, **kwargs
        )
        assert resp.status_code == 401
        assert "not implemented" in resp.json()["detail"]

    def test_real_ledger_and_notifier_block_breach(self, uow, clock):
        """Auth stubbed, but the real ledger/notifier: a breach returns 501 and stores nothing."""
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
        app.dependency_overrides.update(
            {
                get_principal: lambda: system_principal(SYSTEM_ID),
                get_uow: lambda: uow,
                get_clock: lambda: clock,
            }
        )
        client = TestClient(app)
        resp = client.post("/v1/metrics", json=VALID_METRIC)
        assert resp.status_code == 501
        assert "app/modules/ledger" in resp.json()["detail"]
        assert uow.metrics.rows == {} and uow.events.rows == {}

        # A non-breaching sample needs neither integration and is stored.
        ok = client.post("/v1/metrics", json={**VALID_METRIC, "value": 0.1})
        assert ok.status_code == 201

    def test_real_ledger_blocks_event_view(self, uow):
        uow.seed_system(SYSTEM_ID)
        from tests.test_event_queries import make_event

        event = make_event(SYSTEM_ID)
        uow.events.rows[event.id] = event
        app = create_app()
        app.dependency_overrides.update(
            {get_principal: lambda: user_principal(Role.VIEWER), get_uow: lambda: uow}
        )
        resp = TestClient(app).get(f"/v1/events/{event.id}")
        assert resp.status_code == 501
