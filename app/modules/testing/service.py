"""POST /v1/test/reset: clean state between cross-team test runs (Section 5.1)."""

from uuid import uuid4

from sqlalchemy import delete, text, update
from sqlalchemy.dialects.postgresql import insert

from app.core.config import get_settings
from app.db.uow import UnitOfWork
from app.modules.systems.models import MonitoredSystem
from app.modules.testing.fixtures import BASELINE_SYSTEMS, FIXTURE_VALID_FROM
from app.modules.testing.models import SimulatedClockState
from app.modules.thresholds.models import ThresholdVersion

# Every table holding events, metrics, notifications or their audit trail. Accounts and API keys
# are kept, so testers' keys survive a reset; monitored systems are kept and reseeded.
RESET_TABLES = (
    "notification_delivery",
    "notification",
    "webhook",
    "ledger_entry",
    "event",
    "threshold_version",
    "metric_sample",
)


class ResetNotAllowed(RuntimeError):
    pass


def reset_test_environment(uow: UnitOfWork) -> None:
    """Clear all events, metrics, webhooks and ledger entries, reseed the baseline fixtures and
    set the simulated clock back to real time. One transaction: all or nothing.

    Only possible in the test environment: this deliberately breaks the append-only ledger.
    """
    if not get_settings().test_routes_enabled:
        raise ResetNotAllowed("test routes are disabled")
    with uow:
        session = uow.session
        if session is None:
            raise RuntimeError("reset needs a database session")
        session.execute(text(f"TRUNCATE {', '.join(RESET_TABLES)}"))
        session.execute(update(MonitoredSystem).values(last_seen_at=None))
        for fixture in BASELINE_SYSTEMS:
            values = {
                "name": fixture.name,
                "sampling_interval_s": fixture.sampling_interval_s,
                "gap_missed_intervals": fixture.gap_missed_intervals,
            }
            session.execute(
                insert(MonitoredSystem)
                .values(id=fixture.id, **values)
                .on_conflict_do_update(index_elements=["id"], set_=values)
            )
            for t in fixture.thresholds:
                session.add(
                    ThresholdVersion(
                        id=uuid4(),
                        system_id=fixture.id,
                        metric_name=t.metric_name,
                        metric_type=t.metric_type.value,
                        direction=t.direction.value,
                        limit_value=t.limit_value,
                        valid_from=FIXTURE_VALID_FROM,
                    )
                )
        session.execute(delete(SimulatedClockState))
        session.flush()
