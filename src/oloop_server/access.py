"""Who may do what in a project (spec/SERVER.md, "Principals")."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from oloop_server.errors import ApiError
from oloop_server.models import ProjectMember

OWNER = "owner"
WRITE = "write"
READ = "read"
WRITERS = (OWNER, WRITE)


def role_of(session: Session, project_id: uuid.UUID, principal_id: uuid.UUID) -> str | None:
    return session.scalar(
        select(ProjectMember.role).where(
            ProjectMember.project_id == project_id,
            ProjectMember.principal_id == principal_id,
        )
    )


def projects_of(principal_id: uuid.UUID):
    """A subquery: the ids of the projects a principal is a member of."""
    return select(ProjectMember.project_id).where(ProjectMember.principal_id == principal_id)


def not_found() -> ApiError:
    # The same answer for "there is none" and "it is not yours", so the
    # existence of a project is not revealed (spec/SERVER.md, "API v1").
    return ApiError(404, "not_found", "not found")


def require_read(session: Session, project_id: uuid.UUID, principal_id: uuid.UUID) -> str:
    role = role_of(session, project_id, principal_id)
    if role is None:
        raise not_found()
    return role


def require_write(session: Session, project_id: uuid.UUID, principal_id: uuid.UUID) -> str:
    role = require_read(session, project_id, principal_id)
    if role not in WRITERS:
        raise ApiError(403, "forbidden", "this project is read-only for you")
    return role


def require_owner(session: Session, project_id: uuid.UUID, principal_id: uuid.UUID) -> None:
    if require_read(session, project_id, principal_id) != OWNER:
        raise ApiError(403, "forbidden", "only the owner of the project may do this")
