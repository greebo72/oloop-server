"""What an agent does about itself: renew its token and rotate its key pair
(spec/AGENTS.md, "Renewing the token" and "Rotating the key pair")."""
import uuid
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oloop_server.agentauth import issue_token, newest_token, replace_tokens
from oloop_server.agents import PUBLIC_KEY_BYTES
from oloop_server.auth import Principal, SessionDep, current_principal
from oloop_server.db import utcnow
from oloop_server.documents import INT_MAX, WRAPPED_PROJECT_KEY_BYTES, iso
from oloop_server.encoding import b64url_decode
from oloop_server.errors import ApiError
from oloop_server.models import Agent, AgentKey, ProjectKey

router = APIRouter(prefix="/v1/agent")

# crypto_box_easy of newPublicKey | agentId | keyNumber: nonce, 52 bytes, tag.
STATEMENT_BYTES = 24 + 32 + 16 + 4 + 16


def current_agent(principal: Annotated[Principal, Depends(current_principal)]) -> Principal:
    if principal.kind != "agent":
        raise ApiError(403, "forbidden", "this is for agents")
    return principal


AgentDep = Annotated[Principal, Depends(current_agent)]


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _binary(text: str, length: int, what: str) -> bytes:
    try:
        raw = b64url_decode(text)
    except ValueError:
        raise _bad(f"{what} is not base64url without padding") from None
    if len(raw) != length:
        raise _bad(f"{what} must be {length} bytes")
    return raw


def _renewal(session: Session, agent: Agent, shown, now: datetime) -> datetime:
    """The expiry a renewed token would get, or 403 when the token cannot be
    renewed any further (the renewal period is used up, or it was never one)."""
    newest = newest_token(session, agent)
    expires_at = min(now + timedelta(seconds=agent.lifetime_seconds), agent.renew_until)
    # A token that was replaced and is shown again within its grace belongs to an
    # answer that was lost. Giving it a token of the same expiry is no extension.
    retry = shown.replaced_at is not None
    if expires_at < newest.expires_at or (expires_at == newest.expires_at and not retry):
        raise ApiError(403, "forbidden", "the token cannot be renewed any further")
    return expires_at


def _renew(session: Session, agent: Agent, shown, now: datetime) -> tuple[str, datetime]:
    expires_at = _renewal(session, agent, shown, now)
    replace_tokens(session, agent, now, at_once=False)
    return issue_token(session, agent, now, expires_at), expires_at


@router.post("/renew")
def renew(principal: AgentDep, session: SessionDep):
    now = utcnow()
    agent = session.get(Agent, principal.id)
    token, expires_at = _renew(session, agent, principal.agent_token, now)
    session.commit()
    return {"token": token, "expiresAt": iso(expires_at)}


# ---------------- Rotating the key pair ----------------

class ProjectKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projectId: uuid.UUID
    keyVersion: int = Field(ge=1, le=INT_MAX)
    wrapped: str


class KeypairIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publicKey: str
    statement: str
    keys: list[ProjectKeyIn] = Field(max_length=10000)


@router.put("/keypair")
def rotate_keypair(body: KeypairIn, principal: AgentDep, session: SessionDep):
    """All or nothing: the new public key, the statement of the old key for it, and
    the key of every project and key version the agent has, handed to itself."""
    now = utcnow()
    agent = session.get(Agent, principal.id)
    public_key = _binary(body.publicKey, PUBLIC_KEY_BYTES, "publicKey")
    statement = _binary(body.statement, STATEMENT_BYTES, "statement")
    if public_key == agent.public_key:
        raise _bad("publicKey is the one the agent has")

    held = session.scalars(select(ProjectKey).where(ProjectKey.principal_id == agent.id)).all()
    given = {}
    for key in body.keys:
        slot = (key.projectId, key.keyVersion)
        if slot in given:
            raise _bad("a key is given twice")
        given[slot] = _binary(key.wrapped, WRAPPED_PROJECT_KEY_BYTES, "wrapped")
    if set(given) != {(k.project_id, k.key_version) for k in held}:
        raise _bad("keys must hold every project key of the agent, and only those")

    expires_at = _renewal(session, agent, principal.agent_token, now)

    number = (session.scalar(
        select(func.max(AgentKey.number)).where(AgentKey.agent_id == agent.id)) or 1) + 1
    agent.public_key = public_key
    session.add(AgentKey(agent_id=agent.id, number=number, public_key=public_key,
                         statement=statement, created_at=now))
    for key in held:
        key.wrapped = given[(key.project_id, key.key_version)]
        key.granted_by = agent.id

    replace_tokens(session, agent, now, at_once=False)
    token = issue_token(session, agent, now, expires_at)
    try:
        session.commit()
    except IntegrityError:
        # Another rotation of the same agent was faster.
        session.rollback()
        raise ApiError(409, "conflict", "the key pair changed meanwhile, try again") from None
    return {"token": token, "expiresAt": iso(expires_at)}
