"""MetricRepository.in_window on PostgreSQL: the evidence-bundle query (Model 2; D-30)."""

from datetime import timedelta
from decimal import Decimal

from app.modules.metrics.models import MetricSample
from app.modules.metrics.repository import SqlMetricRepository
from app.modules.review.evidence import build_evidence_bundle
from tests.conftest import T0

H = timedelta(hours=1)


def test_in_window_matches_the_fake(pg_sessionmaker, seed):
    system, _, _ = seed()
    other, _, _ = seed()
    times = [T0 - H - timedelta(microseconds=1), T0 - H, T0, T0 + H, T0 + H + timedelta(seconds=1)]
    with pg_sessionmaker.begin() as s:
        for i, at in enumerate(times):
            s.add(
                MetricSample(
                    system_id=system,
                    metric_name="accuracy",
                    sampled_at=at,
                    metric_type="performance",
                    value=Decimal(f"0.{60 + i}"),
                    received_at=at,
                )
            )
        for sid, name in [(system, "psi"), (other, "accuracy")]:
            s.add(
                MetricSample(
                    system_id=sid,
                    metric_name=name,
                    sampled_at=T0,
                    metric_type="drift",
                    value=Decimal("0.3"),
                    received_at=T0,
                )
            )
    with pg_sessionmaker() as s:
        repo = SqlMetricRepository(s)
        rows = repo.in_window(system, "accuracy", T0 - H, T0 + H)
        assert [r.sampled_at for r in rows] == [T0 - H, T0, T0 + H]
        bundle = build_evidence_bundle(
            repo, system_id=system, metric_name="accuracy", breach_at=T0, assembled_at=T0 + 2 * H
        )
    assert [x["value"] for x in bundle.samples] == [0.61, 0.62, 0.63]
