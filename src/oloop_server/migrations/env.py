from alembic import context
from sqlalchemy import engine_from_config, pool

import oloop_server.models  # noqa: F401  (registers the tables)
from oloop_server.db import Base

config = context.config
target_metadata = Base.metadata


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    owns_connection = connectable is None
    if owns_connection:
        engine = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        connectable = engine.connect()

    try:
        context.configure(connection=connectable, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if owns_connection:
            connectable.close()


if context.is_offline_mode():
    raise RuntimeError("offline migrations are not supported")

run_migrations_online()
