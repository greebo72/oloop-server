"""The tokens of agents: issuing and finding them (spec/AGENTS.md, "Renewing the token")."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from oloop_server.models import Agent, AgentToken
from oloop_server.security import hash_token, new_token

# A replaced token is valid this much longer, so that a lost answer to a renewal
# does not lock the agent out.
GRACE = timedelta(minutes=10)


def issue_token(session: Session, agent: Agent, now: datetime, expires_at: datetime) -> str:
    """Makes a token for the agent and returns it. Only its hash is kept."""
    token, token_hash = new_token("oloop_ag")
    session.add(AgentToken(agent_id=agent.id, token_hash=token_hash,
                           created_at=now, expires_at=expires_at))
    return token


def find_token(session: Session, token: str, now: datetime) -> tuple[Agent, AgentToken] | None:
    """The agent and the token row, when the token is valid now: it exists, has not
    expired, was not replaced longer ago than the grace, and the agent is not revoked."""
    row = session.scalar(select(AgentToken).where(AgentToken.token_hash == hash_token(token)))
    if row is None or row.expires_at <= now:
        return None
    if row.replaced_at is not None and row.replaced_at + GRACE <= now:
        return None
    agent = session.get(Agent, row.agent_id)
    if agent is None or agent.revoked_at is not None:
        return None
    return agent, row


def newest_token(session: Session, agent: Agent) -> AgentToken | None:
    return session.scalar(
        select(AgentToken).where(AgentToken.agent_id == agent.id)
        .order_by(AgentToken.created_at.desc(), AgentToken.id).limit(1)
    )


def replace_tokens(session: Session, agent: Agent, now: datetime, *, at_once: bool) -> None:
    """Marks the tokens that are not replaced yet as replaced. With `at_once` they
    stop working now, without they work for the grace."""
    stamp = now - GRACE if at_once else now
    for row in session.scalars(select(AgentToken).where(
            AgentToken.agent_id == agent.id, AgentToken.replaced_at.is_(None))):
        row.replaced_at = stamp
