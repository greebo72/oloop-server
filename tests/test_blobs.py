import hashlib
import os
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from conftest import bearer, login
from helpers import (
    add_member,
    data,
    make_project,
    make_task,
    new_id,
    task_body,
    write,
)
from oloop_server import cli
from oloop_server.blobstore import blob_path, collect_garbage
from oloop_server.config import get_settings
from oloop_server.db import utcnow
from oloop_server.models import Blob


def blob_bytes(fill=1, size=100) -> bytes:
    """Something with the shape of an envelope; the server cannot tell more."""
    return bytes([1, 1]) + bytes([fill]) * (size - 2)


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def upload(client, headers, blob_id, body, task_id, *, digest=None, **extra):
    return client.put(
        f"/v1/blobs/{blob_id}", content=body,
        headers={**headers, "X-Content-SHA256": digest or sha(body), "X-Oloop-Task": task_id,
                 "Content-Type": "application/octet-stream", **extra})


@pytest.fixture
def task(client, alice):
    project_id = make_project(client, alice)
    return project_id, make_task(client, alice, project_id)


def files_below(folder):
    return sorted(p for p in folder.rglob("*") if p.is_file())


def row(app, blob_id):
    with Session(app.state.engine) as session:
        return session.get(Blob, uuid.UUID(blob_id))


# ---------------- Without a login ----------------

@pytest.mark.parametrize("method", ["get", "head", "put"])
def test_every_blob_call_needs_a_login(client, method):
    assert getattr(client, method)(f"/v1/blobs/{uuid.uuid4()}").status_code == 401


# ---------------- Uploading and downloading ----------------

def test_a_blob_is_stored_and_comes_back_byte_for_byte(client, alice, task, app):
    _, task_id = task
    blob_id, body = new_id(), blob_bytes(7, 5000)

    response = upload(client, alice, blob_id, body, task_id)

    assert response.status_code == 201
    got = client.get(f"/v1/blobs/{blob_id}", headers=alice)
    assert got.status_code == 200 and got.content == body
    assert got.headers["content-type"] == "application/octet-stream"
    assert got.headers["etag"] == f'"{sha(body)}"'
    stored = blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id))
    assert stored.read_bytes() == body
    assert stored.parent.parent.parent == app.state.settings.blob_dir
    assert row(app, blob_id).sha256 == sha(body) and row(app, blob_id).size == 5000


def test_head_has_the_headers_and_no_body(client, alice, task):
    _, task_id = task
    blob_id, body = new_id(), blob_bytes(2, 321)
    upload(client, alice, blob_id, body, task_id)

    response = client.head(f"/v1/blobs/{blob_id}", headers=alice)

    assert response.status_code == 200 and response.content == b""
    assert response.headers["etag"] == f'"{sha(body)}"'
    assert response.headers["content-length"] == "321"


def test_a_cache_with_the_etag_gets_304(client, alice, task):
    _, task_id = task
    blob_id, body = new_id(), blob_bytes()
    upload(client, alice, blob_id, body, task_id)

    response = client.get(f"/v1/blobs/{blob_id}",
                          headers={**alice, "If-None-Match": f'"{sha(body)}"'})

    assert response.status_code == 304 and response.content == b""


def test_no_temporary_file_is_left_behind(client, alice, task, app):
    _, task_id = task
    upload(client, alice, new_id(), blob_bytes(), task_id)

    assert not [p for p in files_below(app.state.settings.blob_dir) if p.name.endswith(".tmp")]


def test_the_same_upload_again_is_accepted_and_changes_nothing(client, alice, task, app):
    _, task_id = task
    blob_id, body = new_id(), blob_bytes(3)
    upload(client, alice, blob_id, body, task_id)
    before = row(app, blob_id).created_at

    again = upload(client, alice, blob_id, body, task_id)

    assert again.status_code == 200
    assert row(app, blob_id).created_at == before
    assert len(files_below(app.state.settings.blob_dir)) == 1


def test_a_blob_is_immutable(client, alice, task, app):
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(1), task_id)

    other = upload(client, alice, blob_id, blob_bytes(2), task_id)

    assert other.status_code == 409
    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).content == blob_bytes(1)
    assert len(files_below(app.state.settings.blob_dir)) == 1


def test_the_same_id_for_another_task_is_a_conflict(client, alice, task):
    project_id, task_id = task
    second = make_task(client, alice, project_id)
    blob_id, body = new_id(), blob_bytes()
    upload(client, alice, blob_id, body, task_id)

    assert upload(client, alice, blob_id, body, second).status_code == 409


def test_a_name_that_is_taken_is_never_replaced(client, alice, task, app):
    # a file without a blob: an upload in progress, or what a crash left
    _, task_id = task
    blob_id = new_id()
    final = blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id))
    final.parent.mkdir(parents=True)
    final.write_bytes(b"someone else's")

    response = upload(client, alice, blob_id, blob_bytes(), task_id)

    assert response.status_code == 409
    assert final.read_bytes() == b"someone else's"
    assert row(app, blob_id) is None
    assert files_below(app.state.settings.blob_dir) == [final]


def test_a_blob_whose_file_is_gone_is_restored_by_the_same_upload(client, alice, task, app):
    _, task_id = task
    blob_id, body = new_id(), blob_bytes(4)
    upload(client, alice, blob_id, body, task_id)
    final = blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id))
    final.unlink()

    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).status_code == 404

    assert upload(client, alice, blob_id, body, task_id).status_code == 200
    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).content == body


def test_other_content_does_not_take_the_place_of_a_lost_file(client, alice, task, app):
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(1), task_id)
    blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id)).unlink()

    response = upload(client, alice, blob_id, blob_bytes(2), task_id)

    assert response.status_code == 409
    assert files_below(app.state.settings.blob_dir) == []


# ---------------- What is refused ----------------

def test_a_hash_that_is_not_the_one_of_the_body_stores_nothing(client, alice, task, app):
    _, task_id = task
    blob_id = new_id()

    response = upload(client, alice, blob_id, blob_bytes(), task_id, digest=sha(b"something else"))

    assert response.status_code == 400
    assert row(app, blob_id) is None
    assert files_below(app.state.settings.blob_dir) == []


@pytest.mark.parametrize("digest", ["", "abc", "g" * 64, "a" * 63, "a" * 65])
def test_a_hash_header_that_is_no_sha256_is_refused(client, alice, task, digest):
    _, task_id = task
    response = client.put(
        f"/v1/blobs/{new_id()}", content=blob_bytes(),
        headers={**alice, "X-Content-SHA256": digest, "X-Oloop-Task": task_id})

    assert response.status_code == 400


def test_the_hash_may_be_written_in_capitals(client, alice, task):
    _, task_id = task
    body = blob_bytes()

    assert upload(client, alice, new_id(), body, task_id,
                  digest=sha(body).upper()).status_code == 201


@pytest.mark.parametrize("task_header", [None, "", "not-a-uuid"])
def test_the_task_header_is_needed(client, alice, task_header):
    headers = {**alice, "X-Content-SHA256": sha(blob_bytes())}
    if task_header is not None:
        headers["X-Oloop-Task"] = task_header

    assert client.put(f"/v1/blobs/{new_id()}", content=blob_bytes(),
                      headers=headers).status_code == 400


@pytest.mark.parametrize("body", [
    b"",
    bytes([1, 1]) + b"x" * 39,        # one byte less than an envelope can be
    bytes([2, 1]) + b"x" * 98,
    bytes([1, 2]) + b"x" * 98,
    bytes([0, 0]) + b"x" * 98,
])
def test_a_body_that_is_no_envelope_is_refused(client, alice, task, app, body):
    _, task_id = task
    blob_id = new_id()

    response = upload(client, alice, blob_id, body, task_id)

    assert response.status_code == 400
    assert row(app, blob_id) is None
    assert files_below(app.state.settings.blob_dir) == []


def test_the_least_envelope_is_accepted(client, alice, task):
    _, task_id = task

    assert upload(client, alice, new_id(), blob_bytes(size=42), task_id).status_code == 201


def test_a_body_over_the_limit_is_refused_and_leaves_nothing(client, alice, task, app):
    _, task_id = task
    app.state.settings.max_blob_bytes = 1000
    blob_id = new_id()

    response = upload(client, alice, blob_id, blob_bytes(size=1001), task_id)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"
    assert row(app, blob_id) is None
    assert files_below(app.state.settings.blob_dir) == []


def test_a_body_at_the_limit_is_accepted(client, alice, task, app):
    _, task_id = task
    app.state.settings.max_blob_bytes = 1000

    assert upload(client, alice, new_id(), blob_bytes(size=1000), task_id).status_code == 201


def test_the_limit_holds_for_a_body_that_does_not_say_how_long_it_is(client, alice, task, app):
    # no Content-Length: the server counts while it receives
    _, task_id = task
    app.state.settings.max_blob_bytes = 1000
    body = blob_bytes(size=3000)

    def pieces():
        for start in range(0, len(body), 500):
            yield body[start:start + 500]

    response = client.put(
        f"/v1/blobs/{new_id()}", content=pieces(),
        headers={**alice, "X-Content-SHA256": sha(body), "X-Oloop-Task": task_id})

    assert response.status_code == 413
    assert files_below(app.state.settings.blob_dir) == []


def test_a_large_body_in_pieces_is_stored_whole(client, alice, task):
    _, task_id = task
    body = blob_bytes(9, 300_000)
    blob_id = new_id()

    def pieces():
        for start in range(0, len(body), 65_536):
            yield body[start:start + 65_536]

    response = client.put(
        f"/v1/blobs/{blob_id}", content=pieces(),
        headers={**alice, "X-Content-SHA256": sha(body), "X-Oloop-Task": task_id})

    assert response.status_code == 201
    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).content == body


# ---------------- Who may ----------------

def test_the_task_has_to_exist(client, alice):
    assert upload(client, alice, new_id(), blob_bytes(), new_id()).status_code == 404


def test_a_project_is_no_task(client, alice):
    project_id = make_project(client, alice)

    assert upload(client, alice, new_id(), blob_bytes(), project_id).status_code == 404


def test_another_user_cannot_upload_nor_tell_that_the_task_exists(client, alice, bob, task):
    _, task_id = task

    into_task = upload(client, bob, new_id(), blob_bytes(), task_id)
    nothing = upload(client, bob, new_id(), blob_bytes(), new_id())

    assert into_task.status_code == nothing.status_code == 404
    assert into_task.json() == nothing.json()


def test_another_user_cannot_read_a_blob(client, alice, bob, task):
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)

    assert client.get(f"/v1/blobs/{blob_id}", headers=bob).status_code == 404
    assert client.head(f"/v1/blobs/{blob_id}", headers=bob).status_code == 404
    assert client.get(f"/v1/blobs/{new_id()}", headers=alice).status_code == 404


def test_a_read_member_reads_but_cannot_upload(client, alice, bob, app, task):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    add_member(app, project_id, "bob", "read")

    assert client.get(f"/v1/blobs/{blob_id}", headers=bob).status_code == 200
    refused = upload(client, bob, new_id(), blob_bytes(), task_id)
    assert refused.status_code == 403


def test_a_write_member_uploads(client, alice, bob, app, task):
    project_id, task_id = task
    add_member(app, project_id, "bob", "write")

    assert upload(client, bob, new_id(), blob_bytes(), task_id).status_code == 201


def test_a_signed_out_device_cannot_download(client, alice, task):
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    client.post("/v1/auth/logout", headers=alice)

    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).status_code == 401


# ---------------- A task refers to a blob ----------------

def refer(client, headers, project_id, task_id, rev, blob_ref, base):
    return write(client, headers, task_id,
                 task_body(project_id, rev=rev, blobRef=blob_ref), base=base)


def test_a_task_refers_to_its_blob_and_can_let_go_of_it(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    assert row(app, blob_id).unreferenced_since is not None
    assert row(app, blob_id).first_referenced_at is None

    assert refer(client, alice, project_id, task_id, 2, blob_id, 1).status_code == 200

    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["blobRef"] == blob_id
    assert row(app, blob_id).unreferenced_since is None
    assert row(app, blob_id).first_referenced_at is not None

    assert refer(client, alice, project_id, task_id, 3, None, 2).status_code == 200
    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["blobRef"] is None
    assert row(app, blob_id).unreferenced_since is not None
    assert row(app, blob_id).first_referenced_at is not None


def test_a_task_can_swap_one_blob_for_another(client, alice, task, app):
    project_id, task_id = task
    first, second = new_id(), new_id()
    upload(client, alice, first, blob_bytes(1), task_id)
    upload(client, alice, second, blob_bytes(2), task_id)
    refer(client, alice, project_id, task_id, 2, first, 1)

    assert refer(client, alice, project_id, task_id, 3, second, 2).status_code == 200

    assert row(app, first).unreferenced_since is not None
    assert row(app, second).unreferenced_since is None


def test_writing_a_task_that_keeps_its_blob_changes_nothing_about_it(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)
    first = row(app, blob_id).first_referenced_at

    refer(client, alice, project_id, task_id, 3, blob_id, 2)

    assert row(app, blob_id).first_referenced_at == first
    assert row(app, blob_id).unreferenced_since is None


def test_a_blob_that_does_not_exist_cannot_be_referred_to(client, alice, task):
    project_id, task_id = task

    response = refer(client, alice, project_id, task_id, 2, new_id(), 1)

    assert response.status_code == 400
    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["rev"] == 1


def test_a_blob_of_another_task_cannot_be_referred_to(client, alice, task):
    project_id, task_id = task
    other_task = make_task(client, alice, project_id)
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), other_task)

    assert refer(client, alice, project_id, task_id, 2, blob_id, 1).status_code == 400


def test_a_task_cannot_be_created_with_a_blob(client, alice, task):
    project_id, _ = task

    response = write(client, alice, new_id(), task_body(project_id, blobRef=new_id()))

    assert response.status_code == 400


def test_a_refused_write_leaves_the_books_of_the_blobs_alone(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)

    # a conflict: the revision is old
    assert refer(client, alice, project_id, task_id, 2, None, 1).status_code == 409

    assert row(app, blob_id).unreferenced_since is None


def test_a_restore_refers_to_the_blob_again(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)
    refer(client, alice, project_id, task_id, 3, None, 2)
    assert row(app, blob_id).unreferenced_since is not None

    assert refer(client, alice, project_id, task_id, 4, blob_id, 3).status_code == 200

    assert row(app, blob_id).unreferenced_since is None


# ---------------- Clean-up ----------------

def clean(app, **kwargs):
    return collect_garbage(app.state.session_factory, app.state.settings.blob_dir, **kwargs)


def test_a_blob_nobody_used_goes_after_24_hours_with_its_file(client, alice, task, app):
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    final = blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id))

    young = clean(app, now=utcnow() + timedelta(hours=23))
    assert young.never_referenced == 0 and final.exists() and row(app, blob_id) is not None

    old = clean(app, now=utcnow() + timedelta(hours=25))
    assert old.never_referenced == 1
    assert not final.exists() and row(app, blob_id) is None
    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).status_code == 404


def test_a_blob_in_use_stays_however_old_it_is(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)

    result = clean(app, now=utcnow() + timedelta(days=400))

    assert (result.never_referenced, result.dropped) == (0, 0)
    assert client.get(f"/v1/blobs/{blob_id}", headers=alice).status_code == 200


def test_a_dropped_blob_goes_30_days_after_it_was_dropped(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)
    refer(client, alice, project_id, task_id, 3, None, 2)
    final = blob_path(app.state.settings.blob_dir, uuid.UUID(blob_id))

    # 24 hours are not enough for a blob that was used once
    assert clean(app, now=utcnow() + timedelta(days=29)).dropped == 0 and final.exists()

    assert clean(app, now=utcnow() + timedelta(days=31)).dropped == 1
    assert not final.exists() and row(app, blob_id) is None


def test_a_blob_taken_up_again_in_time_stays(client, alice, task, app):
    project_id, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    refer(client, alice, project_id, task_id, 2, blob_id, 1)
    refer(client, alice, project_id, task_id, 3, None, 2)
    refer(client, alice, project_id, task_id, 4, blob_id, 3)

    result = clean(app, now=utcnow() + timedelta(days=60))

    assert result.dropped == 0 and row(app, blob_id) is not None


def test_a_blob_is_taken_by_the_clean_up_only_if_the_rule_still_holds(client, alice, task, app):
    # the blob was chosen as unused; a task takes it up before it is deleted
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    with Session(app.state.engine) as session:
        blob = session.get(Blob, uuid.UUID(blob_id))
        blob.first_referenced_at = utcnow()
        session.commit()

    assert clean(app, now=utcnow() + timedelta(hours=48)).never_referenced == 0
    assert row(app, blob_id) is not None


def test_only_old_temporary_files_and_files_of_no_blob_are_swept(client, alice, task, app):
    _, task_id = task
    folder = app.state.settings.blob_dir
    kept_id = new_id()
    upload(client, alice, kept_id, blob_bytes(), task_id)
    shard = blob_path(folder, uuid.UUID(kept_id)).parent
    old_tmp = shard / ".old.tmp"
    young_tmp = shard / ".young.tmp"
    orphan = shard / str(uuid.uuid4())
    young_orphan = shard / str(uuid.uuid4())
    foreign = shard / "notes.txt"
    for path in (old_tmp, young_tmp, orphan, young_orphan, foreign):
        path.write_bytes(b"x")
    two_days_ago = time.time() - 2 * 86400
    for path in (old_tmp, orphan, foreign):
        os.utime(path, (two_days_ago, two_days_ago))

    result = clean(app)

    assert result.stray_files == 2
    assert not old_tmp.exists() and not orphan.exists()
    assert young_tmp.exists() and young_orphan.exists()
    assert foreign.exists(), "a file that is not ours is left alone"
    assert blob_path(folder, uuid.UUID(kept_id)).exists()


def test_the_clean_up_of_an_empty_folder_is_fine(app):
    assert clean(app).stray_files == 0


def test_the_command_line_cleans_up_once(settings, monkeypatch, capsys, make_user):
    monkeypatch.setenv("OLOOP_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("OLOOP_BLOB_DIR", str(settings.blob_dir))
    get_settings.cache_clear()
    try:
        from oloop_server.main import create_app
        from oloop_server.migrate import upgrade
        upgrade(settings.database_url)
        make_user("alice")
        app = create_app(get_settings())
        with TestClient(app) as client:
            headers = bearer(login(client).json()["accessToken"])
            project_id = make_project(client, headers)
            task_id = make_task(client, headers, project_id)
            blob_id = new_id()
            assert upload(client, headers, blob_id, blob_bytes(), task_id).status_code == 201
        with Session(app.state.engine) as session:
            session.get(Blob, uuid.UUID(blob_id)).created_at = utcnow() - timedelta(days=2)
            session.commit()

        assert cli.main(["gc-blobs"]) == 0

        assert "removed 1 blobs never used, 0 dropped ones and 0 stray files" in capsys.readouterr().out
        assert row(app, blob_id) is None
    finally:
        get_settings.cache_clear()


def test_the_server_cleans_up_by_itself(app, make_user):
    app.state.settings.blob_gc_interval_minutes = 0.0005  # 30 ms
    make_user("alice")

    with TestClient(app) as client:
        headers = bearer(login(client).json()["accessToken"])
        project_id = make_project(client, headers)
        task_id = make_task(client, headers, project_id)
        blob_id = new_id()
        upload(client, headers, blob_id, blob_bytes(), task_id)
        with Session(app.state.engine) as session:
            session.get(Blob, uuid.UUID(blob_id)).created_at = utcnow() - timedelta(days=2)
            session.commit()

        for _ in range(100):
            if row(app, blob_id) is None:
                break
            time.sleep(0.05)

    assert row(app, blob_id) is None


def test_the_clean_up_can_be_switched_off(app, make_user):
    app.state.settings.blob_gc_interval_minutes = 0
    make_user("alice")

    with TestClient(app) as client:
        headers = bearer(login(client).json()["accessToken"])
        project_id = make_project(client, headers)
        task_id = make_task(client, headers, project_id)
        blob_id = new_id()
        upload(client, headers, blob_id, blob_bytes(), task_id)
        with Session(app.state.engine) as session:
            session.get(Blob, uuid.UUID(blob_id)).created_at = utcnow() - timedelta(days=2)
            session.commit()
        time.sleep(0.3)

    assert row(app, blob_id) is not None


def test_blobs_are_listed_by_the_database_only_after_their_file_is_there(client, alice, task, app):
    # the order the spec asks for: file first, then the row
    _, task_id = task
    blob_id = new_id()
    upload(client, alice, blob_id, blob_bytes(), task_id)
    with Session(app.state.engine) as session:
        rows = session.scalars(select(Blob)).all()

    assert len(rows) == 1
    assert blob_path(app.state.settings.blob_dir, rows[0].id).is_file()


def test_a_data_document_unrelated_to_blobs_is_not_affected(client, alice, task):
    project_id, task_id = task

    assert write(client, alice, task_id, task_body(project_id, rev=2, data=data(5)),
                 base=1).status_code == 200
