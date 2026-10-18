"""Engine and session management."""

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def create_app_engine(url: URL | str, *, pool_size: int = 5, max_overflow: int = 5) -> Engine:
    """Engine with the application's connection settings (also used by integration tests)."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        # Session time zone pinned to UTC so timestamptz values come back as UTC datetimes.
        connect_args={"connect_timeout": 5, "options": "-c timezone=UTC"},
    )


def create_app_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: services return ORM objects after their unit of work has closed.
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_app_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return create_app_sessionmaker(get_engine())


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request.

    Services own the transaction boundary (`with db.begin(): ...`) so that a state change, its
    ledger entry and its notification outbox row commit together (Phase 2, Q7.2 Decision 1).
    """
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> None:
    """Raise if the database is unreachable. Used by /healthz."""
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))
