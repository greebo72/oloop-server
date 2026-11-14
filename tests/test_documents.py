import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from helpers import (
    add_member,
    b64,
    data,
    make_project,
    make_task,
    new_id,
    project_body,
    task_body,
    user_id,
    wrapped,
    write,
)
from oloop_server import documents
from oloop_server.models import Counter

VECTORS = Path(__file__).parent.parent / "spec" / "fixtures" / "server" / "vectors.json"


# ---------------- Without a login ----------------

@pytest.mark.parametrize("method,path", [
    ("get", "/v1/head"), ("get", "/v1/projects"), ("get", "/v1/documents"),
    ("get", f"/v1/documents/{uuid.uuid4()}"), ("put", f"/v1/documents/{uuid.uuid4()}"),
    ("get", f"/v1/documents/{uuid.uuid4()}/revisions"),
    ("get", f"/v1/documents/{uuid.uuid4()}/revisions/1"),
])
def test_everything_needs_a_login(client, method, path):
    assert getattr(client, method)(path).status_code == 401


# ---------------- Creating a project ----------------

def test_a_project_is_created_by_writing_its_document(client, alice, app):
    project_id = new_id()

    response = write(client, alice, project_id, project_body())

    assert response.status_code == 200
    assert response.json()["rev"] == 1
    assert response.json()["cursor"].isdigit()

    projects = client.get("/v1/projects", headers=alice).json()
    assert projects == [{
        "id": project_id, "role": "owner",
        "keys": [{"keyVersion": 1, "grantedBy": str(user_id(app, "alice")),
                  "wrapped": wrapped()}],
    }]


def test_the_document_of_a_project_comes_back_unchanged(client, alice, app):
    project_id = new_id()
    write(client, alice, project_id, project_body(data=data(7, 120)))

    doc = client.get(f"/v1/documents/{project_id}", headers=alice).json()

    assert doc["id"] == project_id and doc["kind"] == "project"
    assert doc["rev"] == 1 and doc["keyVersion"] == 1 and doc["size"] == 120
    assert doc["data"] == data(7, 120)
    assert doc["writtenBy"] == str(user_id(app, "alice"))
    assert "projectId" not in doc and "blobRef" not in doc
    assert doc["updatedAt"].endswith("Z")


def test_the_project_of_the_test_vectors_is_accepted(client, alice):
    # Made by another implementation: sizes and spelling against theirs.
    vectors = json.loads(VECTORS.read_text())
    project = vectors["documents"]["cases"][0]
    grant = vectors["grants"]["cases"][0]
    envelope = b64(bytes.fromhex(project["envelopeHex"]))
    key = b64(bytes.fromhex(grant["wrappedHex"]))

    response = write(client, alice, project["id"], project_body(
        data=envelope, key={"keyVersion": 1, "wrapped": key}))

    assert response.status_code == 200
    assert client.get(f"/v1/documents/{project['id']}", headers=alice).json()["data"] == envelope
    assert client.get("/v1/projects", headers=alice).json()[0]["keys"][0]["wrapped"] == key


@pytest.mark.parametrize("changes", [
    {"key": None},
    {"key": {"keyVersion": 2, "wrapped": wrapped()}},
    {"keyVersion": 2},
    {"key": {"keyVersion": 1, "wrapped": b64(b"x" * 91)}},
    {"key": {"keyVersion": 1, "wrapped": b64(b"x" * 93)}},
    {"key": {"keyVersion": 1, "wrapped": "not base64!"}},
    {"projectId": "00000000-0000-4000-8000-000000000001"},
    {"blobRef": "00000000-0000-4000-8000-000000000001"},
    {"rev": 2},
    {"data": b64(bytes([2, 1]) + b"x" * 78)},
    {"data": b64(bytes([1, 2]) + b"x" * 78)},
    {"data": b64(b"\x01\x01" + b"x" * 30)},
    {"data": "not base64!"},
    {"extra": 1},
])
def test_a_project_that_is_wrong_is_refused_and_leaves_nothing(client, alice, changes):
    response = write(client, alice, new_id(), project_body(**changes))

    assert response.status_code == 400
    assert client.get("/v1/projects", headers=alice).json() == []
    assert client.get("/v1/head", headers=alice).json() == {"cursor": "0"}


def test_the_project_may_name_itself_as_its_project(client, alice):
    project_id = new_id()

    assert write(client, alice, project_id,
                 project_body(projectId=project_id)).status_code == 200


def test_a_document_over_the_limit_is_refused_with_413(client, alice):
    limit = 256 * 1024

    response = write(client, alice, new_id(), project_body(data=data(1, limit + 1)))

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "too_large"


def test_a_document_at_the_limit_is_accepted(client, alice):
    assert write(client, alice, new_id(),
                 project_body(data=data(1, 256 * 1024))).status_code == 200


# ---------------- The preconditions ----------------

def test_a_second_create_with_the_same_id_is_a_conflict(client, alice):
    project_id = make_project(client, alice)

    response = write(client, alice, project_id, project_body())

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


def test_a_change_of_a_document_that_does_not_exist_is_not_found(client, alice):
    assert write(client, alice, new_id(), project_body(rev=2), base=1).status_code == 404


def test_a_write_needs_exactly_one_precondition(client, alice):
    body = project_body()
    url = f"/v1/documents/{new_id()}"

    assert client.put(url, json=body, headers=alice).status_code == 400
    assert client.put(url, json=body, headers={
        **alice, "If-Match": '"1"', "If-None-Match": "*"}).status_code == 400


def test_a_new_document_has_rev_1(client, alice):
    project_id = make_project(client, alice)

    response = write(client, alice, new_id(), task_body(project_id, rev=2))

    assert response.status_code == 400


# ---------------- Tasks ----------------

def test_a_task_is_created_changed_and_read(client, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)

    changed = write(client, alice, task_id, task_body(project_id, rev=2, fill=2), base=1)

    assert changed.status_code == 200 and changed.json()["rev"] == 2
    doc = client.get(f"/v1/documents/{task_id}", headers=alice).json()
    assert doc["rev"] == 2 and doc["data"] == data(2)
    assert doc["projectId"] == project_id and doc["blobRef"] is None


def test_a_change_on_an_old_revision_is_a_conflict(client, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2, fill=2), base=1)

    response = write(client, alice, task_id, task_body(project_id, rev=2, fill=3), base=1)

    assert response.status_code == 409
    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["data"] == data(2)


def test_rev_must_be_the_next_one(client, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)

    assert write(client, alice, task_id, task_body(project_id, rev=3), base=1).status_code == 400
    assert write(client, alice, task_id, task_body(project_id, rev=1), base=1).status_code == 400


def test_a_task_needs_its_project(client, alice):
    assert write(client, alice, new_id(), {
        "kind": "task", "keyVersion": 1, "rev": 1, "data": data()}).status_code == 400
    assert write(client, alice, new_id(), task_body(new_id())).status_code == 404


def test_a_task_cannot_carry_a_key(client, alice):
    project_id = make_project(client, alice)

    response = write(client, alice, new_id(), task_body(
        project_id, key={"keyVersion": 1, "wrapped": wrapped()}))

    assert response.status_code == 400


def test_a_task_cannot_use_a_key_version_the_project_does_not_have(client, alice):
    project_id = make_project(client, alice)

    assert write(client, alice, new_id(), task_body(
        project_id, keyVersion=2)).status_code == 400


def test_the_project_of_a_task_and_the_kind_of_a_document_cannot_change(client, alice):
    one = make_project(client, alice)
    other = make_project(client, alice)
    task_id = make_task(client, alice, one)

    assert write(client, alice, task_id, task_body(other, rev=2), base=1).status_code == 400
    assert write(client, alice, task_id, project_body(rev=2, key=None), base=1).status_code == 400


def test_a_project_changes_with_a_new_revision(client, alice):
    project_id = make_project(client, alice)

    response = write(client, alice, project_id, project_body(rev=2, key=None, data=data(5)), base=1)

    assert response.status_code == 200
    assert client.get(f"/v1/documents/{project_id}", headers=alice).json()["data"] == data(5)


def test_the_key_is_only_for_the_creation_of_a_project(client, alice):
    project_id = make_project(client, alice)

    response = write(client, alice, project_id, project_body(rev=2), base=1)

    assert response.status_code == 400


# ---------------- Who may see and write ----------------

def test_another_user_sees_nothing_of_a_project(client, alice, bob):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)

    assert client.get("/v1/projects", headers=bob).json() == []
    assert client.get(f"/v1/documents/{project_id}", headers=bob).status_code == 404
    assert client.get(f"/v1/documents/{task_id}", headers=bob).status_code == 404
    assert client.get(f"/v1/documents/{task_id}/revisions", headers=bob).status_code == 404
    assert client.get(f"/v1/documents/{task_id}/revisions/1", headers=bob).status_code == 404
    assert client.get("/v1/documents", headers=bob).json()["items"] == []


def test_another_user_cannot_write_into_a_project_and_cannot_tell_it_exists(client, alice, bob):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)

    into_project = write(client, bob, new_id(), task_body(project_id))
    change = write(client, bob, task_id, task_body(project_id, rev=2), base=1)
    nothing = write(client, bob, new_id(), task_body(new_id()))

    assert into_project.status_code == change.status_code == nothing.status_code == 404
    assert into_project.json() == nothing.json()


def test_a_read_member_reads_and_cannot_write(client, alice, bob, app):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    add_member(app, project_id, "bob", "read")

    assert client.get(f"/v1/documents/{task_id}", headers=bob).status_code == 200
    assert client.get("/v1/projects", headers=bob).json()[0]["role"] == "read"
    create = write(client, bob, new_id(), task_body(project_id))
    change = write(client, bob, task_id, task_body(project_id, rev=2), base=1)

    assert create.status_code == change.status_code == 403
    assert create.json()["error"]["code"] == "forbidden"


def test_a_write_member_writes_and_is_recorded_as_the_writer(client, alice, bob, app):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    add_member(app, project_id, "bob", "write")

    assert write(client, bob, task_id, task_body(project_id, rev=2), base=1).status_code == 200

    doc = client.get(f"/v1/documents/{task_id}", headers=alice).json()
    assert doc["writtenBy"] == str(user_id(app, "bob"))


# ---------------- Head and feed ----------------

def test_the_head_is_zero_before_anything_is_written(client, alice):
    assert client.get("/v1/head", headers=alice).json() == {"cursor": "0"}


def test_the_head_follows_every_write_and_only_for_what_the_user_sees(client, alice, bob):
    project_id = make_project(client, alice)
    first = client.get("/v1/head", headers=alice).json()["cursor"]
    written = write(client, alice, new_id(), task_body(project_id)).json()["cursor"]

    assert int(written) > int(first)
    assert client.get("/v1/head", headers=alice).json() == {"cursor": written}
    assert client.get("/v1/head", headers=bob).json() == {"cursor": "0"}


def test_the_numbers_of_the_changes_rise_without_gaps(client, alice, app):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2), base=1)

    with Session(app.state.engine) as session:
        assert session.get(Counter, "changes").value == 3
    assert client.get("/v1/head", headers=alice).json() == {"cursor": "3"}


def test_a_refused_write_does_not_use_up_a_number(client, alice, app):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2), base=1)
    write(client, alice, task_id, task_body(project_id, rev=2, fill=3), base=1)  # conflict

    with Session(app.state.engine) as session:
        assert session.get(Counter, "changes").value == 3


def test_the_feed_without_a_cursor_loads_everything_oldest_change_first(client, alice):
    project_id = make_project(client, alice)
    one = make_task(client, alice, project_id)
    two = make_task(client, alice, project_id)

    feed = client.get("/v1/documents", headers=alice).json()

    assert [d["id"] for d in feed["items"]] == [project_id, one, two]
    assert feed["hasMore"] is False
    assert feed["nextCursor"] == client.get("/v1/head", headers=alice).json()["cursor"]
    assert feed["items"][1]["data"] == data()


def test_the_feed_with_a_cursor_holds_only_what_changed_since(client, alice):
    project_id = make_project(client, alice)
    one = make_task(client, alice, project_id)
    two = make_task(client, alice, project_id)
    cursor = client.get("/v1/documents", headers=alice).json()["nextCursor"]

    write(client, alice, one, task_body(project_id, rev=2, fill=4), base=1)
    feed = client.get("/v1/documents", params={"cursor": cursor}, headers=alice).json()

    # a document shows up once, in its current state
    assert [d["id"] for d in feed["items"]] == [one]
    assert feed["items"][0]["rev"] == 2 and feed["items"][0]["data"] == data(4)
    assert two not in [d["id"] for d in feed["items"]]
    nothing = client.get("/v1/documents", params={"cursor": feed["nextCursor"]}, headers=alice).json()
    assert nothing == {"items": [], "nextCursor": feed["nextCursor"], "hasMore": False}


def test_a_document_changed_twice_is_in_the_feed_once(client, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2), base=1)
    write(client, alice, task_id, task_body(project_id, rev=3), base=2)

    feed = client.get("/v1/documents", headers=alice).json()

    assert [d["id"] for d in feed["items"]].count(task_id) == 1
    assert next(d for d in feed["items"] if d["id"] == task_id)["rev"] == 3


def test_the_feed_is_read_in_pages(client, alice):
    project_id = make_project(client, alice)
    ids = [project_id] + [make_task(client, alice, project_id) for _ in range(4)]

    seen, cursor, pages = [], "", 0
    while True:
        page = client.get("/v1/documents", params={"cursor": cursor, "limit": 2}, headers=alice).json()
        seen += [d["id"] for d in page["items"]]
        cursor, pages = page["nextCursor"], pages + 1
        if not page["hasMore"]:
            break

    assert seen == ids and pages == 3


def test_a_page_ends_at_its_size_but_holds_at_least_one_document(client, alice, monkeypatch):
    project_id = make_project(client, alice)
    make_task(client, alice, project_id)
    make_task(client, alice, project_id)
    monkeypatch.setattr(documents, "FEED_MAX_BYTES", 100)

    first = client.get("/v1/documents", headers=alice).json()

    assert len(first["items"]) == 1 and first["hasMore"] is True
    second = client.get("/v1/documents", params={"cursor": first["nextCursor"]}, headers=alice).json()
    assert len(second["items"]) == 1 and second["hasMore"] is True


@pytest.mark.parametrize("cursor", ["abc", "-1", "1.5", "9" * 40, " 1"])
def test_a_cursor_the_server_did_not_give_is_refused(client, alice, cursor):
    assert client.get("/v1/documents", params={"cursor": cursor}, headers=alice).status_code == 400


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_the_limit_of_a_page_has_bounds(client, alice, limit):
    assert client.get("/v1/documents", params={"limit": limit}, headers=alice).status_code == 400


def test_the_feed_holds_only_documents_of_the_users_projects(client, alice, bob):
    mine = make_project(client, alice)
    make_task(client, alice, mine)
    theirs = make_project(client, bob)

    assert [d["id"] for d in client.get("/v1/documents", headers=bob).json()["items"]] == [theirs]


# ---------------- History ----------------

def test_every_revision_is_kept_and_can_be_read(client, alice, app):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2, fill=2), base=1)
    write(client, alice, task_id, task_body(project_id, rev=3, fill=3), base=2)

    listing = client.get(f"/v1/documents/{task_id}/revisions", headers=alice).json()

    assert [r["rev"] for r in listing] == [1, 2, 3]
    assert all(r["writtenBy"] == str(user_id(app, "alice")) for r in listing)
    assert all(r["keyVersion"] == 1 and r["at"].endswith("Z") for r in listing)
    old = client.get(f"/v1/documents/{task_id}/revisions/2", headers=alice).json()
    assert old["rev"] == 2 and old["data"] == data(2) and old["id"] == task_id
    assert client.get(f"/v1/documents/{task_id}/revisions/9", headers=alice).status_code == 404
    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["rev"] == 3


def test_a_restore_is_an_ordinary_write_of_the_old_content(client, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    write(client, alice, task_id, task_body(project_id, rev=2, fill=2), base=1)
    old = client.get(f"/v1/documents/{task_id}/revisions/1", headers=alice).json()

    restored = write(client, alice, task_id, task_body(
        project_id, rev=3, data=old["data"]), base=2)

    assert restored.status_code == 200
    assert client.get(f"/v1/documents/{task_id}", headers=alice).json()["data"] == data(1)


def test_a_document_that_does_not_exist_is_not_found(client, alice):
    missing = new_id()

    for path in (f"/v1/documents/{missing}", f"/v1/documents/{missing}/revisions",
                 f"/v1/documents/{missing}/revisions/1"):
        response = client.get(path, headers=alice)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_an_id_that_is_no_uuid_is_a_bad_request(client, alice):
    assert client.get("/v1/documents/not-a-uuid", headers=alice).status_code == 400
