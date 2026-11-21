"""What the tests of projects, documents and blobs have in common."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from oloop_server.encoding import b64url_encode
from oloop_server.models import ProjectKey, ProjectMember, User


def b64(data: bytes) -> str:
    return b64url_encode(data)


def data(fill=1, size=80) -> str:
    """Something with the shape of an envelope. The server cannot tell more."""
    return b64(bytes([1, 1]) + bytes([fill]) * (size - 2))


def wrapped(fill=9) -> str:
    return b64(bytes([fill]) * 92)


def new_id() -> str:
    return str(uuid.uuid4())


def write(client, headers, doc_id, body, *, base=None):
    """base None creates (If-None-Match), a number changes (If-Match)."""
    precondition = {"If-None-Match": "*"} if base is None else {"If-Match": f'"{base}"'}
    return client.put(f"/v1/documents/{doc_id}", json=body, headers={**headers, **precondition})


def project_body(**changes):
    body = {"kind": "project", "keyVersion": 1, "rev": 1, "data": data(),
            "key": {"keyVersion": 1, "wrapped": wrapped()}}
    body.update(changes)
    return body


def task_body(project_id, rev=1, fill=1, **changes):
    body = {"kind": "task", "projectId": project_id, "keyVersion": 1, "rev": rev,
            "data": data(fill)}
    body.update(changes)
    return body


def make_project(client, headers):
    project_id = new_id()
    assert write(client, headers, project_id, project_body()).status_code == 200
    return project_id


def make_task(client, headers, project_id):
    task_id = new_id()
    assert write(client, headers, task_id, task_body(project_id)).status_code == 200
    return task_id


def user_id(app, name):
    with Session(app.state.engine) as session:
        return session.scalar(select(User.id).where(User.username_key == name))


def add_member(app, project_id, name, role):
    """The API for more members comes with the agents; the rows are what the
    rules of access are tested with."""
    with Session(app.state.engine) as session:
        uid = session.scalar(select(User.id).where(User.username_key == name))
        session.add(ProjectMember(project_id=uuid.UUID(project_id), principal_id=uid,
                                  kind="user", role=role))
        session.flush()
        session.add(ProjectKey(project_id=uuid.UUID(project_id), principal_id=uid,
                               key_version=1, granted_by=uid, wrapped=b"\x00" * 92))
        session.commit()


def agent_body(name="claude", **changes):
    """The form of a registration; the server cannot tell more."""
    body = {"id": new_id(), "name": name, "publicKey": b64(bytes([7]) * 32), "registration": b64(bytes([8]) * 88)}
    body.update(changes)
    return body


def make_agent(client, headers, name="claude", **changes):
    """Creates an agent and returns the answer: agentId, token and expiresAt."""
    response = client.post("/v1/agents", json=agent_body(name, **changes), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def add_agent_member(app, project_id, agent_id, role):
    """The rows of a member that is an agent; the API for it comes after."""
    with Session(app.state.engine) as session:
        agent = uuid.UUID(agent_id)
        session.add(ProjectMember(project_id=uuid.UUID(project_id), principal_id=agent,
                                  kind="agent", role=role))
        session.flush()
        session.add(ProjectKey(project_id=uuid.UUID(project_id), principal_id=agent,
                               key_version=1, granted_by=agent, wrapped=b"\x00" * 92))
        session.commit()
