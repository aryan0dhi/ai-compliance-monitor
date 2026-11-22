"""A report's evidence bundle: the Model 2 ±1 h window around the breach (REQ-05; D-05, D-30)."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.modules.metrics.models import MetricSample
from app.modules.review.evidence import build_evidence_bundle, evidence_window
from tests.conftest import T0
from tests.fakes import FakeMetricRepository

SYSTEM = UUID("550e8400-e29b-41d4-a716-446655440000")
OTHER_SYSTEM = UUID("660e8400-e29b-41d4-a716-446655440000")
H = timedelta(hours=1)


def sample(at, value="0.9", *, system=SYSTEM, metric="accuracy", ref=None):
    return MetricSample(
        system_id=system,
        metric_name=metric,
        sampled_at=at,
        metric_type="performance",
        value=Decimal(value),
        snapshot_ref=ref,
        received_at=at,
    )


@pytest.fixture
def metrics():
    repo = FakeMetricRepository()
    for s in [
        sample(T0 - H - timedelta(seconds=1)),  # just before the window
        sample(T0 - H, "0.88"),  # the start is included
        sample(T0 - timedelta(minutes=5), "0.75"),
        sample(T0, "0.61", ref="snap-1"),  # the breach
        sample(T0 + H, "0.66"),  # the end is included
        sample(T0 + H + timedelta(seconds=1)),  # just after
        sample(T0, "0.1", metric="psi"),  # another metric
        sample(T0, "0.2", system=OTHER_SYSTEM),  # another system
    ]:
        repo.add_if_absent(s)
    return repo


def bundle(metrics, assembled_at):
    return build_evidence_bundle(
        metrics, system_id=SYSTEM, metric_name="accuracy", breach_at=T0, assembled_at=assembled_at
    )


class TestWindow:
    def test_one_hour_each_side_once_that_hour_has_passed(self):
        assert evidence_window(T0, T0 + 3 * H) == (T0 - H, T0 + H)

    def test_it_ends_at_assembly_inside_the_first_hour(self):
        at = T0 + timedelta(minutes=20)
        assert evidence_window(T0, at) == (T0 - H, at)

    def test_it_never_ends_before_the_breach(self):
        """A sample time ahead of the service clock (the system's clock runs fast)."""
        assert evidence_window(T0, T0 - timedelta(minutes=3)) == (T0 - H, T0)


class TestBundle:
    def test_samples_in_the_window_oldest_first_bounds_included(self, metrics):
        b = bundle(metrics, T0 + 2 * H)
        assert b.as_json() == {
            "window": {"start": "2026-09-27T19:00:00Z", "end": "2026-09-27T21:00:00Z"},
            "samples": [
                {"sampled_at": "2026-09-27T19:00:00Z", "value": 0.88, "snapshot_ref": None},
                {"sampled_at": "2026-09-27T19:55:00Z", "value": 0.75, "snapshot_ref": None},
                {"sampled_at": "2026-09-27T20:00:00Z", "value": 0.61, "snapshot_ref": "snap-1"},
                {"sampled_at": "2026-09-27T21:00:00Z", "value": 0.66, "snapshot_ref": None},
            ],
        }

    def test_an_early_report_has_only_the_samples_up_to_assembly(self, metrics):
        b = bundle(metrics, T0 + timedelta(minutes=30))
        assert [s["value"] for s in b.samples] == [0.88, 0.75, 0.61]
        assert b.as_json()["window"]["end"] == "2026-09-27T20:30:00Z"

    def test_the_hash_is_over_canonical_json(self, metrics):
        b = bundle(metrics, T0 + 2 * H)
        canonical = json.dumps(b.as_json(), sort_keys=True, separators=(",", ":"))
        assert b.sha256 == hashlib.sha256(canonical.encode()).hexdigest()
        assert b.sha256 == bundle(metrics, T0 + 5 * H).sha256  # same window, same hash

    def test_the_triggering_sample_must_be_there(self):
        with pytest.raises(ValueError, match="triggering sample"):
            bundle(FakeMetricRepository(), T0 + 2 * H)

    def test_microseconds_keep_six_digits(self):
        repo = FakeMetricRepository()
        at = T0.replace(microsecond=120)
        repo.add_if_absent(sample(at))
        b = build_evidence_bundle(
            repo, system_id=SYSTEM, metric_name="accuracy", breach_at=at, assembled_at=at + H
        )
        assert b.samples[0]["sampled_at"] == "2026-09-27T20:00:00.000120Z"
