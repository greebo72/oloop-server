"""Removes tokens that can never be used again."""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from oloop_server.agentauth import GRACE, newest_token
from oloop_server.db import utcnow
from oloop_server.models import Agent, AgentToken, Token

# What is dead is kept this long, so a clock that is a little off cannot remove a
# token that is still good.
MARGIN = timedelta(days=1)


@dataclass
class Purged:
    user_tokens: int
    agent_tokens: int


def purge_expired_tokens(session_factory: sessionmaker[Session], now: datetime | None = None) -> Purged:
    """Deletes the tokens of users that have expired and the tokens of agents that
    have expired or were replaced for good. The newest token of an agent stays even
    when it has expired: the list of agents shows when it did."""
    now = now or utcnow()
    with session_factory() as session:
        user_tokens = session.execute(
            delete(Token).where(Token.expires_at < now - MARGIN)).rowcount

        agent_tokens = 0
        dead = (AgentToken.expires_at < now - MARGIN) | (AgentToken.replaced_at < now - GRACE - MARGIN)
        for agent_id in set(session.scalars(select(AgentToken.agent_id).where(dead))):
            keep = newest_token(session, session.get(Agent, agent_id))
            agent_tokens += session.execute(
                delete(AgentToken).where(AgentToken.agent_id == agent_id, dead,
                                         AgentToken.id != keep.id)).rowcount
        session.commit()
    return Purged(user_tokens=user_tokens, agent_tokens=agent_tokens)
