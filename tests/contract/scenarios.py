"""Helpers shared by the contract tests: sample times, timestamp parsing and the B-22 error body."""

from datetime import UTC, datetime, timedelta

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
