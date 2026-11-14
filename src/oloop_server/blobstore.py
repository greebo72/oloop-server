"""Where the encrypted blobs live, and the removal of the ones nobody needs.

A blob is a file below the blob folder, `<ab>/<cd>/<id>` after the first
characters of its id, so no folder grows large. The file is complete before
the blob is known to the database.
"""
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from oloop_server.db import utcnow
from oloop_server.models import Blob

# spec/SERVER.md, "Images": how long a blob is kept that no task refers to.
NEVER_REFERENCED_AFTER = timedelta(hours=24)
DROPPED_AFTER = timedelta(days=30)

# A file that is no blob and no upload in progress is removed after this long.
STRAY_FILE_AFTER = timedelta(hours=24)


def blob_path(folder: Path, blob_id: uuid.UUID) -> Path:
    name = str(blob_id)
    return folder / name[:2] / name[2:4] / name


@dataclass
class GcResult:
    never_referenced: int = 0
    dropped: int = 0
    stray_files: int = 0


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def collect_garbage(
    sessions: sessionmaker[Session], folder: Path, now: datetime | None = None
) -> GcResult:
    """Removes the blobs that are not needed any more (spec/SERVER.md, "Images").

    The row goes first, in a step that checks the rule again: a task that
    refers to the blob in the meantime keeps it. A crash between the row and
    the file leaves a file, which the sweep of stray files takes later.
    """
    now = now or utcnow()
    result = GcResult()

    with sessions() as session:
        never = session.scalars(select(Blob.id).where(
            Blob.first_referenced_at.is_(None),
            Blob.created_at < now - NEVER_REFERENCED_AFTER,
        )).all()
        dropped = session.scalars(select(Blob.id).where(
            Blob.first_referenced_at.is_not(None),
            Blob.unreferenced_since.is_not(None),
            Blob.unreferenced_since < now - DROPPED_AFTER,
        )).all()

        for blob_id in never:
            if session.execute(delete(Blob).where(
                Blob.id == blob_id,
                Blob.first_referenced_at.is_(None),
                Blob.created_at < now - NEVER_REFERENCED_AFTER,
            )).rowcount == 1:
                session.commit()
                _remove_file(blob_path(folder, blob_id))
                result.never_referenced += 1

        for blob_id in dropped:
            if session.execute(delete(Blob).where(
                Blob.id == blob_id,
                Blob.unreferenced_since.is_not(None),
                Blob.unreferenced_since < now - DROPPED_AFTER,
            )).rowcount == 1:
                session.commit()
                _remove_file(blob_path(folder, blob_id))
                result.dropped += 1

        result.stray_files = _sweep_stray_files(session, folder, now)

    return result


def _sweep_stray_files(session: Session, folder: Path, now: datetime) -> int:
    """Removes temporary files of aborted uploads, and files that belong to no
    blob, when they are old enough not to be an upload in progress."""
    if not folder.is_dir():
        return 0

    cutoff = (now - STRAY_FILE_AFTER).timestamp()
    removed = 0
    for current, _dirs, files in os.walk(folder):
        for name in files:
            path = Path(current) / name
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
            except FileNotFoundError:
                continue

            if name.endswith(".tmp"):
                stray = True
            else:
                try:
                    stray = session.get(Blob, uuid.UUID(name)) is None
                except ValueError:
                    stray = False  # not ours: leave it alone

            if stray:
                _remove_file(path)
                removed += 1
    return removed
