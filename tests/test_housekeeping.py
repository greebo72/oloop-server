from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from conftest import bearer
from helpers import make_agent
from oloop_server import cli
from oloop_server.config import get_settings
from oloop_server.housekeeping import MARGIN, purge_expired_tokens
from oloop_server.models import AgentToken, Token


def factory(app):
    return sessionmaker(app.state.engine)


def long_ago():
    return datetime.now(UTC) - MARGIN - timedelta(hours=1)


def test_expired_tokens_of_users_go_and_valid_ones_stay(client, app, alice, db):
    old = db.scalar(select(Token).where(Token.kind == "access"))
    old.expires_at = long_ago()
    db.commit()

    purged = purge_expired_tokens(factory(app))

    assert purged.user_tokens == 1
    # The refresh token is valid, so the user can still renew the session.
    assert [t.kind for t in db.scalars(select(Token))] == ["refresh"]


def test_a_token_that_expired_a_moment_ago_stays_because_of_the_margin(client, app, alice, db):
    token = db.scalar(select(Token).where(Token.kind == "access"))
    token.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    db.commit()

    assert purge_expired_tokens(factory(app)).user_tokens == 0


def test_replaced_and_expired_tokens_of_an_agent_go_but_the_newest_stays(client, app, alice, db):
    made = make_agent(client, alice)
    client.post("/v1/agent/renew", headers=bearer(made["token"]))
    client.post(f"/v1/agents/{made['agentId']}/token", headers=alice)
    assert len(db.scalars(select(AgentToken)).all()) == 3

    for row in db.scalars(select(AgentToken)):
        if row.replaced_at is not None:
            row.replaced_at = long_ago()
    db.commit()
    purged = purge_expired_tokens(factory(app))

    assert purged.agent_tokens == 2
    db.expire_all()
    assert len(db.scalars(select(AgentToken)).all()) == 1


def test_the_newest_token_of_an_agent_stays_even_when_it_expired(client, app, alice, db):
    make_agent(client, alice)
    only = db.scalar(select(AgentToken))
    only.expires_at = long_ago()
    db.commit()

    assert purge_expired_tokens(factory(app)).agent_tokens == 0
    assert client.get("/v1/agents", headers=alice).json()[0]["expiresAt"] is not None


def test_a_token_that_is_valid_or_in_its_grace_is_not_removed(client, app, alice, db):
    made = make_agent(client, alice)
    client.post("/v1/agent/renew", headers=bearer(made["token"]))

    assert purge_expired_tokens(factory(app)).agent_tokens == 0
    assert client.get("/v1/head", headers=bearer(made["token"])).status_code == 200


def test_the_command_does_it_once(settings, monkeypatch, capsys):
    monkeypatch.setenv("OLOOP_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        assert cli.main(["purge-tokens"]) == 0
    finally:
        get_settings.cache_clear()
    assert "removed 0 tokens of users and 0 tokens of agents" in capsys.readouterr().out
