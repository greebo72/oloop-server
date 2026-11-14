"""Documents: projects and tasks, encrypted (spec/SERVER.md, "API v1")."""
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oloop_server import access
from oloop_server.auth import SessionDep, current_device
from oloop_server.db import utcnow
from oloop_server.encoding import b64url_decode, b64url_encode
from oloop_server.errors import ApiError
from oloop_server.models import (
    Counter,
    Device,
    Document,
    Project,
    ProjectKey,
    ProjectMember,
    Revision,
)
from oloop_server.preconditions import read_precondition

router = APIRouter(prefix="/v1")

DeviceDep = Annotated[Device, Depends(current_device)]

# A project key handed over with crypto_box_easy: 24 byte nonce, the message
# (32 byte key, 16 byte project id, 4 byte key version) and the 16 byte tag.
WRAPPED_PROJECT_KEY_BYTES = 24 + 32 + 16 + 4 + 16

# An envelope has at least its header, the nonce and the tag.
MIN_ENVELOPE_BYTES = 2 + 24 + 16

# A page of the feed holds documents up to about this many bytes (at least one).
FEED_MAX_BYTES = 4 * 1024 * 1024
FEED_DEFAULT_LIMIT = 100
FEED_MAX_LIMIT = 500

INT_MAX = 2**31 - 1


class KeyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyVersion: int = Field(ge=1, le=INT_MAX)
    wrapped: str


class PutDocumentIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project", "task"]
    projectId: uuid.UUID | None = None
    keyVersion: int = Field(ge=1, le=INT_MAX)
    rev: int = Field(ge=1, le=INT_MAX)
    blobRef: uuid.UUID | None = None
    data: str
    key: KeyIn | None = None


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------- What a document looks like ----------------

def document_json(doc: Document, rev: Revision) -> dict:
    out = {"id": str(doc.id), "kind": doc.kind}
    if doc.kind == "task":
        out["projectId"] = str(doc.project_id)
    out["keyVersion"] = rev.key_version
    out["rev"] = rev.rev
    if doc.kind == "task":
        out["blobRef"] = str(rev.blob_ref) if rev.blob_ref else None
    out["size"] = rev.size
    out["writtenBy"] = str(rev.written_by)
    out["updatedAt"] = iso(rev.written_at)
    out["data"] = b64url_encode(rev.data)
    return out


def _current():
    """The join of a document with its current revision."""
    return select(Document, Revision).join(
        Revision, (Revision.document_id == Document.id) & (Revision.rev == Document.rev)
    )


def _decode_envelope(text: str, limit: int) -> bytes:
    try:
        data = b64url_decode(text)
    except ValueError:
        raise _bad("data is not base64url without padding") from None
    if len(data) > limit:
        raise ApiError(413, "too_large", f"a document may be at most {limit} bytes")
    if len(data) < MIN_ENVELOPE_BYTES:
        raise _bad("data is too short for an envelope")
    if data[0] != 1 or data[1] != 1:
        raise _bad("data is not an envelope of version 1 with algorithm 1")
    return data


def _next_seq(session: Session) -> int:
    """Takes the next number of a change. See `Counter`."""
    return session.execute(
        update(Counter).where(Counter.name == "changes")
        .values(value=Counter.value + 1).returning(Counter.value)
    ).scalar_one()


def _newest_key_version(session: Session, project_id: uuid.UUID) -> int:
    return session.scalar(
        select(func.max(ProjectKey.key_version)).where(ProjectKey.project_id == project_id)
    ) or 0


# ---------------- Reading ----------------

@router.get("/head")
def head(device: DeviceDep, session: SessionDep):
    """The number of the latest change the caller can see. Cheap to ask."""
    newest = session.scalar(
        select(func.max(Document.seq)).where(Document.project_id.in_(access.projects_of(device.user_id)))
    )
    return {"cursor": str(newest or 0)}


@router.get("/documents")
def feed(
    device: DeviceDep,
    session: SessionDep,
    cursor: str = "",
    limit: Annotated[int, Query(ge=1, le=FEED_MAX_LIMIT)] = FEED_DEFAULT_LIMIT,
):
    """The documents that changed after `cursor`, oldest change first. An empty
    cursor starts at the beginning, so the same call loads everything."""
    if cursor == "":
        after = 0
    elif cursor.isdigit() and len(cursor) <= 18:
        after = int(cursor)
    else:
        raise _bad("cursor is not one this server gave")

    rows = session.execute(
        _current()
        .where(Document.seq > after, Document.project_id.in_(access.projects_of(device.user_id)))
        .order_by(Document.seq)
        .limit(limit + 1)
    ).all()

    items, total, has_more = [], 0, len(rows) > limit
    for doc, rev in rows[:limit]:
        if items and total + rev.size > FEED_MAX_BYTES:
            has_more = True
            break
        items.append((doc, rev))
        total += rev.size

    return {
        "items": [document_json(doc, rev) for doc, rev in items],
        "nextCursor": str(items[-1][0].seq if items else after),
        "hasMore": has_more,
    }


def _load(session: Session, doc_id: uuid.UUID, device: Device) -> tuple[Document, Revision]:
    row = session.execute(_current().where(Document.id == doc_id)).first()
    if row is None:
        raise access.not_found()
    access.require_read(session, row[0].project_id, device.user_id)
    return row


@router.get("/documents/{doc_id}")
def get_document(doc_id: uuid.UUID, device: DeviceDep, session: SessionDep):
    doc, rev = _load(session, doc_id, device)
    return document_json(doc, rev)


@router.get("/documents/{doc_id}/revisions")
def list_revisions(doc_id: uuid.UUID, device: DeviceDep, session: SessionDep):
    doc, _ = _load(session, doc_id, device)
    revisions = session.scalars(
        select(Revision).where(Revision.document_id == doc.id).order_by(Revision.rev)
    )
    return [
        {"rev": r.rev, "keyVersion": r.key_version, "writtenBy": str(r.written_by),
         "at": iso(r.written_at)}
        for r in revisions
    ]


@router.get("/documents/{doc_id}/revisions/{rev}")
def get_revision(doc_id: uuid.UUID, rev: int, device: DeviceDep, session: SessionDep):
    doc, _ = _load(session, doc_id, device)
    revision = session.get(Revision, (doc.id, rev))
    if revision is None:
        raise access.not_found()
    return document_json(doc, revision)


# ---------------- Writing ----------------

@router.put("/documents/{doc_id}")
def put_document(doc_id: uuid.UUID, body: PutDocumentIn, request: Request,
                 device: DeviceDep, session: SessionDep):
    settings = request.app.state.settings
    base_rev = read_precondition(request)
    data = _decode_envelope(body.data, settings.max_document_bytes)
    now = utcnow()

    if base_rev is None:
        return _create(session, device, doc_id, body, data, now)
    return _update(session, device, doc_id, body, data, base_rev, now)


def _revision(doc_id, body: PutDocumentIn, data: bytes, device: Device, now) -> Revision:
    return Revision(
        document_id=doc_id, rev=body.rev, key_version=body.keyVersion,
        blob_ref=body.blobRef, size=len(data), written_by=device.user_id,
        written_at=now, data=data,
    )


def _create(session: Session, device: Device, doc_id: uuid.UUID, body: PutDocumentIn,
            data: bytes, now: datetime) -> dict:
    if body.rev != 1:
        raise _bad("a new document has rev 1")
    if session.get(Document, doc_id) is not None:
        raise ApiError(409, "conflict", "a document with this id exists already")

    if body.kind == "project":
        project_id = _new_project(session, device, doc_id, body, now)
    else:
        project_id = _task_project(session, device, body)
        if body.key is not None:
            raise _bad("key is only for a new project")

    seq = _next_seq(session)
    session.add(Document(id=doc_id, kind=body.kind, project_id=project_id, rev=1, seq=seq,
                         created_at=now, updated_at=now))
    session.flush()
    session.add(_revision(doc_id, body, data, device, now))

    try:
        session.commit()
    except IntegrityError:
        # The same id, written by a request that was faster.
        session.rollback()
        raise ApiError(409, "conflict", "a document with this id exists already") from None
    return {"rev": 1, "cursor": str(seq)}


def _new_project(session: Session, device: Device, doc_id: uuid.UUID,
                 body: PutDocumentIn, now: datetime) -> uuid.UUID:
    if body.projectId is not None and body.projectId != doc_id:
        raise _bad("projectId of a project is its own id")
    if body.blobRef is not None:
        raise _bad("blobRef is only for a task")
    if body.key is None:
        raise _bad("a new project needs key: the project key handed to its owner")
    if body.keyVersion != 1 or body.key.keyVersion != 1:
        raise _bad("a new project starts with keyVersion 1")

    try:
        wrapped = b64url_decode(body.key.wrapped)
    except ValueError:
        raise _bad("key.wrapped is not base64url without padding") from None
    if len(wrapped) != WRAPPED_PROJECT_KEY_BYTES:
        raise _bad(f"key.wrapped must be {WRAPPED_PROJECT_KEY_BYTES} bytes")

    session.add(Project(id=doc_id, owner_id=device.user_id, created_at=now))
    session.flush()
    session.add(ProjectMember(project_id=doc_id, principal_id=device.user_id, kind="user",
                              role=access.OWNER, created_at=now))
    session.flush()
    session.add(ProjectKey(project_id=doc_id, principal_id=device.user_id, key_version=1,
                           granted_by=device.user_id, wrapped=wrapped, created_at=now))
    return doc_id


def _task_project(session: Session, device: Device, body: PutDocumentIn) -> uuid.UUID:
    if body.projectId is None:
        raise _bad("a task needs projectId")
    access.require_write(session, body.projectId, device.user_id)
    if body.keyVersion > _newest_key_version(session, body.projectId):
        raise _bad("keyVersion is not one of this project")
    return body.projectId


def _update(session: Session, device: Device, doc_id: uuid.UUID, body: PutDocumentIn,
            data: bytes, base_rev: int, now: datetime) -> dict:
    doc = session.get(Document, doc_id)
    if doc is None:
        raise access.not_found()
    access.require_write(session, doc.project_id, device.user_id)

    if body.kind != doc.kind:
        raise _bad("the kind of a document cannot change")
    if body.kind == "task" and body.projectId != doc.project_id:
        raise _bad("the project of a task cannot change")
    if body.kind == "project" and body.projectId not in (None, doc.id):
        raise _bad("projectId of a project is its own id")
    if body.key is not None:
        raise _bad("key is only for a new project")
    if body.kind == "project" and body.blobRef is not None:
        raise _bad("blobRef is only for a task")
    if body.rev != base_rev + 1:
        raise _bad("rev must be the revision of If-Match plus one")
    if body.keyVersion > _newest_key_version(session, doc.project_id):
        raise _bad("keyVersion is not one of this project")

    if doc.rev != base_rev:
        raise ApiError(409, "conflict", "the document changed, load it again")

    # In one step, so that of two writes on the same revision only one wins.
    seq = _next_seq(session)
    changed = session.execute(
        update(Document).where(Document.id == doc.id, Document.rev == base_rev)
        .values(rev=body.rev, seq=seq, updated_at=now)
    ).rowcount
    if changed != 1:
        session.rollback()
        raise ApiError(409, "conflict", "the document changed, load it again")

    session.add(_revision(doc.id, body, data, device, now))
    session.commit()
    return {"rev": body.rev, "cursor": str(seq)}
