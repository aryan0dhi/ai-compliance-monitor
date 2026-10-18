"""Alembic environment: uses the app's settings and model registry."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    # Keep existing loggers: when migrations run in-process (integration tests), the default
    # would silently disable every app.* logger, including the REQ-04/ledger alerts.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = get_settings().sqlalchemy_url
    if not isinstance(url, str):
        url = url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Integration tests pass an existing connection to a throwaway database via
    # `config.attributes["connection"]` (Alembic cookbook pattern).
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    connectable = create_engine(get_settings().sqlalchemy_url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        _run_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
