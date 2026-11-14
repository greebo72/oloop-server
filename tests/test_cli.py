import io

import pytest
from fastapi.testclient import TestClient

from conftest import PASSWORD, login
from oloop_server import cli
from oloop_server.config import get_settings
from oloop_server.main import create_app


@pytest.fixture
def env(settings, monkeypatch):
    monkeypatch.setenv("OLOOP_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    yield settings
    get_settings.cache_clear()


def create(monkeypatch, username, password):
    monkeypatch.setattr("sys.stdin", io.StringIO(password + "\n"))
    return cli.main(["create-user", username, "--password-stdin"])


def test_a_user_created_on_the_command_line_can_log_in(env, monkeypatch):
    assert create(monkeypatch, "alice", PASSWORD) == 0

    with TestClient(create_app(env)) as client:
        assert login(client).status_code == 200


def test_create_user_sets_up_an_empty_database_itself(env, monkeypatch):
    # nothing has migrated the database yet
    assert create(monkeypatch, "alice", PASSWORD) == 0


def test_a_short_password_is_refused(env, monkeypatch, capsys):
    assert create(monkeypatch, "alice", "too short") == 1

    assert "at least 12" in capsys.readouterr().err


def test_a_second_account_with_the_same_name_is_refused_whatever_the_case(env, monkeypatch, capsys):
    assert create(monkeypatch, "alice", PASSWORD) == 0

    assert create(monkeypatch, "ALICE", PASSWORD) == 1
    assert "exists already" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["", "has space", "ümlaut", "x" * 101, "a/b"])
def test_a_user_name_with_odd_characters_is_refused(env, monkeypatch, name):
    assert create(monkeypatch, name, PASSWORD) == 1


def test_the_password_is_not_stored(env, monkeypatch):
    from sqlalchemy.orm import Session

    from oloop_server.db import make_engine
    from oloop_server.models import User

    create(monkeypatch, "alice", PASSWORD)

    with Session(make_engine(env.database_url)) as session:
        stored = session.query(User).one().password_hash

    assert PASSWORD not in stored
    assert stored.startswith("$argon2id$")
