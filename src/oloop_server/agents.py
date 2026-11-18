"""Agents, as a user manages them (spec/AGENTS.md, "API").

The server checks the form of what it stores. It cannot open a registration or
a statement; the clients check the chain of keys.
"""
import uuid
from datetime import datetime, timedelta
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from oloop_server.agentauth import issue_token, newest_token, replace_tokens
from oloop_server.auth import SessionDep, current_device
from oloop_server.db import utcnow
from oloop_server.documents import iso
from oloop_server.encoding import b64url_decode, b64url_encode
from oloop_server.errors import ApiError
from oloop_server.models import Agent, AgentKey, Device, ProjectKey, ProjectMember

router = APIRouter(prefix="/v1/agents")

DeviceDep = Annotated[Device, Depends(current_device)]

PUBLIC_KEY_BYTES = 32
# crypto_box_easy of agentPublicKey | agentId: nonce, 48 bytes of message, tag.
REGISTRATION_BYTES = 24 + 32 + 16 + 16

DEFAULT_TOKEN_DAYS = 90
DEFAULT_RENEWAL_DAYS = 365
# No token and no renewal period is longer than this, whatever was asked for.
MAX_YEARS = 10


class CreateAgentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    publicKey: str
    registration: str
    expiresAt: datetime | None = None
    renewUntil: datetime | None = None


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def name_key(name: str) -> str:
    return name.strip().casefold()


def _binary(text: str, length: int, what: str) -> bytes:
    try:
        raw = b64url_decode(text)
    except ValueError:
        raise _bad(f"{what} is not base64url without padding") from None
    if len(raw) != length:
        raise _bad(f"{what} must be {length} bytes")
    return raw


def _moment(value: datetime, what: str) -> datetime:
    if value.tzinfo is None:
        raise _bad(f"{what} needs a time zone (Z)")
    return value


def _own_agent(session: Session, user: Device, agent_id: uuid.UUID) -> Agent:
    """An agent of the user. The same 404 for one that is not theirs."""
    agent = session.get(Agent, agent_id)
    if agent is None or agent.owner_id != user.user_id:
        raise ApiError(404, "not_found", "not found")
    return agent


def _expires_at(session: Session, agent: Agent) -> datetime | None:
    token = newest_token(session, agent)
    return token.expires_at if token else None


# ---------------- Create ----------------

@router.post("")
def create_agent(body: CreateAgentIn, device: DeviceDep, session: SessionDep):
    now = utcnow()
    name = body.name.strip()
    if not name:
        raise _bad("name must not be empty")
    key = name_key(name)
    public_key = _binary(body.publicKey, PUBLIC_KEY_BYTES, "publicKey")
    registration = _binary(body.registration, REGISTRATION_BYTES, "registration")

    limit = now + timedelta(days=365 * MAX_YEARS)
    expires_at = _moment(body.expiresAt, "expiresAt") if body.expiresAt else now + timedelta(days=DEFAULT_TOKEN_DAYS)
    if not now < expires_at <= limit:
        raise _bad("expiresAt must be in the future and within 10 years")
    if body.renewUntil is not None:
        renew_until = _moment(body.renewUntil, "renewUntil")
    else:
        renew_until = max(now + timedelta(days=DEFAULT_RENEWAL_DAYS), expires_at)
    if not expires_at <= renew_until <= limit:
        raise _bad("renewUntil must not be before expiresAt and within 10 years")

    taken = session.scalar(select(Agent.id).where(
        Agent.owner_id == device.user_id, Agent.name_key == key, Agent.revoked_at.is_(None)))
    if taken is not None:
        raise ApiError(409, "conflict", "an agent of this name exists already")

    agent = Agent(
        owner_id=device.user_id, name=name, name_key=key, public_key=public_key,
        registration=registration,
        # Rounded up, so that a renewal made at once is never earlier than the first token.
        lifetime_seconds=ceil((expires_at - now).total_seconds()),
        renew_until=renew_until,
        renew_period_seconds=ceil((renew_until - now).total_seconds()),
        created_at=now,
    )
    session.add(agent)
    session.flush()
    token = issue_token(session, agent, now, expires_at)
    session.commit()
    return {"agentId": str(agent.id), "token": token, "expiresAt": iso(expires_at)}


# ---------------- List ----------------

def agent_json(session: Session, agent: Agent) -> dict:
    chain = session.scalars(
        select(AgentKey).where(AgentKey.agent_id == agent.id).order_by(AgentKey.number)
    ).all()
    expires_at = _expires_at(session, agent)
    return {
        "id": str(agent.id),
        "name": agent.name,
        "publicKey": b64url_encode(agent.public_key),
        "registration": b64url_encode(agent.registration),
        "keyChain": [{"publicKey": b64url_encode(k.public_key),
                      "statement": b64url_encode(k.statement)} for k in chain],
        "createdAt": iso(agent.created_at),
        "expiresAt": iso(expires_at) if expires_at else None,
        "renewUntil": iso(agent.renew_until),
        "lastUsedAt": iso(agent.last_used_at) if agent.last_used_at else None,
        "revoked": agent.revoked_at is not None,
    }


@router.get("")
def list_agents(device: DeviceDep, session: SessionDep):
    agents = session.scalars(
        select(Agent).where(Agent.owner_id == device.user_id).order_by(Agent.created_at, Agent.id)
    ).all()
    return [agent_json(session, a) for a in agents]


# ---------------- Revoke and a new token ----------------

@router.post("/{agent_id}/revoke", status_code=204)
def revoke_agent(agent_id: uuid.UUID, device: DeviceDep, session: SessionDep):
    agent = _own_agent(session, device, agent_id)
    if agent.revoked_at is None:
        agent.revoked_at = utcnow()
        # The tokens stop working because the agent is revoked. The memberships go
        # at once; the project keys the agent was given go with them.
        session.execute(delete(ProjectKey).where(ProjectKey.principal_id == agent.id))
        session.execute(delete(ProjectMember).where(ProjectMember.principal_id == agent.id))
        session.commit()
    return Response(status_code=204)


@router.post("/{agent_id}/token")
def new_token_for_agent(agent_id: uuid.UUID, device: DeviceDep, session: SessionDep):
    """A new token, for an agent whose token was lost or has expired. It ends the
    tokens it replaces at once and starts a new renewal period."""
    now = utcnow()
    agent = _own_agent(session, device, agent_id)
    if agent.revoked_at is not None:
        raise ApiError(409, "conflict", "the agent is revoked and stays revoked")

    agent.renew_until = now + timedelta(seconds=agent.renew_period_seconds)
    expires_at = min(now + timedelta(seconds=agent.lifetime_seconds), agent.renew_until)
    replace_tokens(session, agent, now, at_once=True)
    token = issue_token(session, agent, now, expires_at)
    session.commit()
    return {"token": token, "expiresAt": iso(expires_at)}

