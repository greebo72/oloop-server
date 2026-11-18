"""The projects of the signed-in principal, with their keys, and who else may use
them (spec/SERVER.md, "API v1").

A project is created by writing its project document, see documents.py.
"""
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oloop_server import access
from oloop_server.auth import Principal, SessionDep, current_device, current_principal
from oloop_server.db import utcnow
from oloop_server.documents import INT_MAX, WRAPPED_PROJECT_KEY_BYTES, restamp_project
from oloop_server.encoding import b64url_decode, b64url_encode
from oloop_server.errors import ApiError
from oloop_server.models import Agent, Device, KeyBundle, ProjectKey, ProjectMember, User

router = APIRouter(prefix="/v1/projects")

PrincipalDep = Annotated[Principal, Depends(current_principal)]
DeviceDep = Annotated[Device, Depends(current_device)]


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


@router.get("")
def list_projects(principal: PrincipalDep, session: SessionDep):
    members = session.scalars(
        select(ProjectMember)
        .where(ProjectMember.principal_id == principal.id)
        .order_by(ProjectMember.created_at, ProjectMember.project_id)
    ).all()

    keys: dict = {}
    for key in session.scalars(
        select(ProjectKey)
        .where(ProjectKey.principal_id == principal.id)
        .order_by(ProjectKey.key_version)
    ):
        keys.setdefault(key.project_id, []).append({
            "keyVersion": key.key_version,
            "grantedBy": str(key.granted_by),
            "wrapped": b64url_encode(key.wrapped),
        })

    return [
        {"id": str(m.project_id), "role": m.role, "keys": keys.get(m.project_id, [])}
        for m in members
    ]


# ---------------- Members ----------------

class WrappedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyVersion: int = Field(ge=1, le=INT_MAX)
    wrapped: str


class AddMemberIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["write", "read"]
    keys: list[WrappedIn] = Field(min_length=1, max_length=1000)


class MemberKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principalId: uuid.UUID
    wrapped: str


class NewKeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyVersion: int = Field(ge=1, le=INT_MAX)
    members: list[MemberKeyIn] = Field(max_length=1000)


class RevokeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principalId: uuid.UUID
    newKey: NewKeyIn | None = None


def _wrapped(text: str, what: str) -> bytes:
    try:
        raw = b64url_decode(text)
    except ValueError:
        raise _bad(f"{what} is not base64url without padding") from None
    if len(raw) != WRAPPED_PROJECT_KEY_BYTES:
        raise _bad(f"{what} must be {WRAPPED_PROJECT_KEY_BYTES} bytes")
    return raw


def _newest_key_version(session: Session, project_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.max(ProjectKey.key_version)).where(ProjectKey.project_id == project_id)
    ) or 0


@router.get("/{project_id}/members")
def list_members(project_id: uuid.UUID, device: DeviceDep, session: SessionDep):
    access.require_owner(session, project_id, device.user_id)

    out = []
    for member in session.scalars(
        select(ProjectMember).where(ProjectMember.project_id == project_id)
        .order_by(ProjectMember.created_at, ProjectMember.principal_id)
    ):
        if member.kind == "agent":
            agent = session.get(Agent, member.principal_id)
            name, public_key = agent.name, agent.public_key
        else:
            user = session.get(User, member.principal_id)
            bundle = session.get(KeyBundle, member.principal_id)
            name = user.username
            public_key = bundle.public_key if bundle else None
        out.append({
            "principalId": str(member.principal_id), "kind": member.kind, "name": name,
            "role": member.role,
            "publicKey": b64url_encode(public_key) if public_key else None,
        })
    return out


@router.put("/{project_id}/members/{principal_id}", status_code=204)
def put_member(project_id: uuid.UUID, principal_id: uuid.UUID, body: AddMemberIn,
               device: DeviceDep, session: SessionDep):
    """Adds an agent of the owner to the project, or changes its role and adds keys."""
    access.require_owner(session, project_id, device.user_id)

    if session.get(User, principal_id) is not None:
        raise _bad("only agents can be added to a project")
    agent = session.get(Agent, principal_id)
    if agent is None or agent.owner_id != device.user_id or agent.revoked_at is not None:
        raise access.not_found()

    newest = _newest_key_version(session, project_id)
    versions = [k.keyVersion for k in body.keys]
    if len(set(versions)) != len(versions):
        raise _bad("a key version is given twice")
    if max(versions) > newest:
        raise _bad("keyVersion is not one of this project")
    if newest not in versions:
        raise _bad(f"the newest key version ({newest}) is needed")
    wrapped = {k.keyVersion: _wrapped(k.wrapped, "wrapped") for k in body.keys}

    now = utcnow()
    member = session.get(ProjectMember, (project_id, principal_id))
    joined = member is None
    if joined:
        session.add(ProjectMember(project_id=project_id, principal_id=principal_id,
                                  kind="agent", role=body.role, created_at=now))
        session.flush()
    else:
        member.role = body.role

    for version, data in wrapped.items():
        key = session.get(ProjectKey, (project_id, principal_id, version))
        if key is None:
            session.add(ProjectKey(project_id=project_id, principal_id=principal_id,
                                   key_version=version, granted_by=device.user_id,
                                   wrapped=data, created_at=now))
        else:
            key.wrapped, key.granted_by = data, device.user_id

    if joined:
        restamp_project(session, project_id)
    session.commit()
    return Response(status_code=204)


@router.post("/{project_id}/revoke", status_code=204)
def revoke_member(project_id: uuid.UUID, body: RevokeIn, device: DeviceDep, session: SessionDep):
    """Removes a member. With `newKey` the project gets a new key version that the
    remaining members are given, so that what is written afterwards is closed to
    the one that was removed."""
    access.require_owner(session, project_id, device.user_id)

    member = session.get(ProjectMember, (project_id, body.principalId))
    if member is None:
        raise access.not_found()
    if member.role == access.OWNER:
        raise _bad("the owner cannot be removed")

    remaining = set(session.scalars(
        select(ProjectMember.principal_id).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_id != body.principalId)
    ).all())

    new_rows = []
    if body.newKey is not None:
        newest = _newest_key_version(session, project_id)
        if body.newKey.keyVersion != newest + 1:
            raise _bad(f"newKey.keyVersion must be {newest + 1}")
        given = [m.principalId for m in body.newKey.members]
        if len(set(given)) != len(given) or set(given) != remaining:
            raise _bad("newKey must hold a key for every remaining member, and only for them")
        now = utcnow()
        new_rows = [
            ProjectKey(project_id=project_id, principal_id=m.principalId,
                       key_version=body.newKey.keyVersion, granted_by=device.user_id,
                       wrapped=_wrapped(m.wrapped, "wrapped"), created_at=now)
            for m in body.newKey.members
        ]

    session.execute(delete(ProjectKey).where(
        ProjectKey.project_id == project_id, ProjectKey.principal_id == body.principalId))
    session.delete(member)
    session.add_all(new_rows)
    try:
        session.commit()
    except IntegrityError:
        # Another request raised the key version first.
        session.rollback()
        raise ApiError(409, "conflict", "the key version changed meanwhile, try again") from None
    return Response(status_code=204)
