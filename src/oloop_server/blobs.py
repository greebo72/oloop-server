"""Image blobs: encrypted files (spec/SERVER.md, "Blobs")."""
import hashlib
import os
import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from oloop_server import access
from oloop_server.auth import Principal, SessionDep, current_principal
from oloop_server.blobstore import blob_path
from oloop_server.db import utcnow
from oloop_server.errors import ApiError
from oloop_server.models import Blob, Document

router = APIRouter(prefix="/v1/blobs")

PrincipalDep = Annotated[Principal, Depends(current_principal)]

# The fixed parts of an envelope: header, nonce, tag.
MIN_BLOB_BYTES = 2 + 24 + 16

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _too_large(limit: int) -> ApiError:
    return ApiError(413, "too_large", f"a blob may be at most {limit} bytes")


def _header_sha(request: Request) -> str:
    value = request.headers.get("x-content-sha256", "").strip()
    if not _SHA256.match(value):
        raise _bad("X-Content-SHA256 must be the SHA-256 of the body in hex")
    return value.lower()


def _header_task(request: Request) -> uuid.UUID:
    try:
        return uuid.UUID(request.headers.get("x-oloop-task", "").strip())
    except ValueError:
        raise _bad("X-Oloop-Task must be the id of the task the blob is for") from None


def _authorize_upload(session: Session, task_id: uuid.UUID, principal: Principal) -> uuid.UUID:
    """The project of the task, if the caller may write there."""
    task = session.get(Document, task_id)
    if task is None or task.kind != "task":
        raise access.not_found()
    access.require_write(session, task.project_id, principal.id)
    return task.project_id


def _same(row: Blob | None, sha: str, task_id: uuid.UUID) -> bool:
    return row is not None and row.sha256 == sha and row.task_id == task_id


def _store(session: Session, principal: Principal, blob_id: uuid.UUID, task_id: uuid.UUID,
           project_id: uuid.UUID, sha: str, size: int, tmp, final) -> int:
    """Puts the finished temporary file in its place and records the blob.

    The file is linked, not renamed over: a name that is taken is never
    replaced, so two uploads of one id cannot swap each other's file.
    """
    try:
        os.link(tmp, final)
    except FileExistsError:
        row = session.get(Blob, blob_id)
        if _same(row, sha, task_id):
            return 200
        if row is not None:
            raise ApiError(409, "conflict", "this blob exists with other content or for another task")
        raise ApiError(409, "conflict", "this blob is being uploaded")
    except OSError:
        # A file system without hard links. Replacing is all that is left.
        if final.exists():
            raise ApiError(409, "conflict", "this blob exists already") from None
        os.replace(tmp, final)

    session.add(Blob(id=blob_id, project_id=project_id, task_id=task_id,
                     uploaded_by=principal.id, size=size, sha256=sha, created_at=utcnow(),
                     unreferenced_since=utcnow()))
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        row = session.get(Blob, blob_id)
        if _same(row, sha, task_id):
            return 200  # the row was there, its file was not: it is back now
        final.unlink(missing_ok=True)
        raise ApiError(409, "conflict", "this blob exists with other content or for another task") from None
    return 201


@router.put("/{blob_id}")
async def put_blob(blob_id: uuid.UUID, request: Request, principal: PrincipalDep, session: SessionDep):
    settings = request.app.state.settings
    limit = settings.max_blob_bytes
    sha = _header_sha(request)
    task_id = _header_task(request)

    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise _too_large(limit)

    project_id = await run_in_threadpool(_authorize_upload, session, task_id, principal)

    final = blob_path(settings.blob_dir, blob_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.parent / f".{blob_id}.{uuid.uuid4().hex}.tmp"

    hasher = hashlib.sha256()
    size, head = 0, b""
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise _too_large(limit)
                head += chunk[: max(0, 2 - len(head))]
                hasher.update(chunk)
                await run_in_threadpool(out.write, chunk)
            out.flush()
            os.fsync(out.fileno())

        if size < MIN_BLOB_BYTES:
            raise _bad("the body is too short for an envelope")
        if head != b"\x01\x01":
            raise _bad("the body is not an envelope of version 1 with algorithm 1")
        if hasher.hexdigest() != sha:
            raise _bad("X-Content-SHA256 is not the SHA-256 of the body")

        status = await run_in_threadpool(
            _store, session, principal, blob_id, task_id, project_id, sha, size, tmp, final)
    finally:
        tmp.unlink(missing_ok=True)

    return Response(status_code=status)


@router.api_route("/{blob_id}", methods=["GET", "HEAD"])
def get_blob(blob_id: uuid.UUID, request: Request, principal: PrincipalDep, session: SessionDep):
    blob = session.get(Blob, blob_id)
    if blob is None:
        raise access.not_found()
    access.require_read(session, blob.project_id, principal.id)

    path = blob_path(request.app.state.settings.blob_dir, blob_id)
    if not path.is_file():
        raise ApiError(404, "not_found", "the blob is missing")

    etag = f'"{blob.sha256}"'
    if request.headers.get("if-none-match", "").strip() in (etag, f"W/{etag}"):
        return Response(status_code=304, headers={"ETag": etag})
    return FileResponse(path, media_type="application/octet-stream", headers={"ETag": etag})
