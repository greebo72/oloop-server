"""The projects of the signed-in user, with their keys (spec/SERVER.md, "API v1").

A project is created by writing its project document, see documents.py.
"""
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select

from oloop_server.auth import Principal, SessionDep, current_principal
from oloop_server.encoding import b64url_encode
from oloop_server.models import ProjectKey, ProjectMember

router = APIRouter(prefix="/v1/projects")

PrincipalDep = Annotated[Principal, Depends(current_principal)]


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
