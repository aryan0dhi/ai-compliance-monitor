"""Fixtures for tests against a real PostgreSQL.

Each test session creates a throwaway database (`compliance_it_<random>`) on the configured
server, migrates it with Alembic, and drops it afterwards. The configured application database
is never touched. Requires a role allowed to CREATE DATABASE (the docker-compose superuser is).
"""

import contextlib
import uuid
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.db.session import create_app_engine, create_app_sessionmaker
from app.db.uow import SqlAlchemyUnitOfWork
from app.modules.systems.models import MonitoredSystem
from app.modules.thresholds.models import ThresholdVersion
from tests.conftest import T0

ROOT = Path(__file__).resolve().parents[2]
TABLES = [
    "notification_delivery",
    "notification",
    "webhook",
    "ledger_entry",
    "event",
    "threshold_version",
    "metric_sample",
    "api_key",
    "app_user",
    "monitored_system",
    "simulated_clock",
    "worker_heartbeat",
]


def alembic_config(connection) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.attributes["connection"] = connection
    return cfg


def migrate(engine: Engine, target: str = "head", *, downgrade: bool = False) -> None:
    with engine.begin() as conn:
        cfg = alembic_config(conn)
        (command.downgrade if downgrade else command.upgrade)(cfg, target)


@contextlib.contextmanager
def throwaway_database(server_url: URL) -> Iterator[Engine]:
    """Create an empty database, yield an app-configured engine for it, then drop it."""
    name = f"compliance_it_{uuid.uuid4().hex[:12]}"
    admin = create_engine(server_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_app_engine(server_url.set(database=name))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture(scope="session")
def pg_engine(postgres_server_url) -> Iterator[Engine]:
    """Engine for a session-wide database migrated to head."""
    with throwaway_database(postgres_server_url) as engine:
        migrate(engine)
        yield engine


@pytest.fixture
def pg_sessionmaker(pg_engine) -> Iterator[sessionmaker[Session]]:
    """Clean tables before each test."""
    with pg_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(TABLES)}"))
    yield create_app_sessionmaker(pg_engine)


@pytest.fixture
def make_uow(pg_sessionmaker):
    return lambda: SqlAlchemyUnitOfWork(pg_sessionmaker)


@pytest.fixture
def seed(pg_sessionmaker):
    """Insert a monitored system and its drift/accuracy thresholds; returns their ids."""

    def _seed(system_id: uuid.UUID | None = None):
        system_id = system_id or uuid.uuid4()
        drift = ThresholdVersion(
            id=uuid.uuid4(),
            system_id=system_id,
            metric_name="drift_score",
            metric_type="drift",
            direction="above",
            limit_value=Decimal("0.30"),
            valid_from=T0 - timedelta(days=1),
        )
        accuracy = ThresholdVersion(
            id=uuid.uuid4(),
            system_id=system_id,
            metric_name="accuracy",
            metric_type="performance",
            direction="below",
            limit_value=Decimal("0.70"),
            valid_from=T0 - timedelta(days=1),
        )
        with pg_sessionmaker.begin() as session:
            session.add(
                MonitoredSystem(
                    id=system_id, name=f"sys-{system_id.hex[:8]}", sampling_interval_s=300
                )
            )
            session.flush()
            session.add_all([drift, accuracy])
        return system_id, drift.id, accuracy.id

    return _seed
