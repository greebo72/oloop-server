from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from oloop_server.db import Base, make_engine
from oloop_server.migrate import upgrade


def test_the_migrations_build_the_tables_of_the_models(settings):
    upgrade(settings.database_url)

    tables = set(inspect(make_engine(settings.database_url)).get_table_names())

    assert {"users", "devices", "tokens", "login_failures"} <= tables


def test_the_migrations_and_the_models_do_not_differ(settings):
    # A change of a model without a migration fails here.
    upgrade(settings.database_url)

    with make_engine(settings.database_url).connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_upgrading_twice_is_harmless(settings):
    upgrade(settings.database_url)
    upgrade(settings.database_url)


def test_a_database_of_the_login_stage_is_brought_up_to_date_with_its_data(settings):
    from alembic import command
    from sqlalchemy import text

    from oloop_server.migrate import alembic_config

    command.upgrade(alembic_config(settings.database_url), "0001")
    engine = make_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO users (id, username, username_key, password_hash, created_at) "
            "VALUES ('00000000000000000000000000000001', 'alice', 'alice', 'x', "
            "'2026-11-14 10:00:00.000000')"))

    upgrade(settings.database_url)

    inspector = inspect(engine)
    assert "key_bundles" in inspector.get_table_names()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT username FROM users")).scalar() == "alice"
