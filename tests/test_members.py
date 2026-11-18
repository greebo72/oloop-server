from conftest import bearer
from helpers import (
    b64,
    make_agent,
    make_project,
    make_task,
    new_id,
    task_body,
    user_id,
    wrapped,
    write,
)


def grant(client, headers, project_id, agent_id, role="write", versions=(1,), fill=5):
    return client.put(
        f"/v1/projects/{project_id}/members/{agent_id}", headers=headers,
        json={"role": role, "keys": [{"keyVersion": v, "wrapped": wrapped(fill)} for v in versions]})


def revoke(client, headers, project_id, principal_id, new_key=None):
    body = {"principalId": str(principal_id)}
    if new_key is not None:
        body["newKey"] = new_key
    return client.post(f"/v1/projects/{project_id}/revoke", json=body, headers=headers)


def new_key(version, *principals):
    return {"keyVersion": version,
            "members": [{"principalId": str(p), "wrapped": wrapped(version)} for p in principals]}


def setup(client, alice, name="claude"):
    project_id = make_project(client, alice)
    made = make_agent(client, alice, name)
    return project_id, made["agentId"], bearer(made["token"])


# ---------------- Listing the members ----------------

def test_the_owner_lists_the_members_with_their_roles_and_keys(client, app, alice):
    project_id, agent_id, _ = setup(client, alice)
    assert grant(client, alice, project_id, agent_id, "read").status_code == 204

    members = client.get(f"/v1/projects/{project_id}/members", headers=alice).json()

    assert {m["kind"]: m["role"] for m in members} == {"user": "owner", "agent": "read"}
    agent = next(m for m in members if m["kind"] == "agent")
    assert agent["principalId"] == agent_id
    assert agent["name"] == "claude"
    assert agent["publicKey"] == b64(bytes([7]) * 32)
    assert next(m for m in members if m["kind"] == "user")["name"] == "alice"


def test_only_the_owner_manages_members(client, alice, bob):
    project_id, agent_id, agent = setup(client, alice)
    grant(client, alice, project_id, agent_id)

    assert client.get(f"/v1/projects/{project_id}/members", headers=agent).status_code == 403
    assert grant(client, agent, project_id, agent_id).status_code == 403
    assert revoke(client, agent, project_id, agent_id).status_code == 403
    # A project of someone else does not show itself.
    assert client.get(f"/v1/projects/{project_id}/members", headers=bob).status_code == 404
    assert grant(client, bob, project_id, agent_id).status_code == 404
    assert revoke(client, bob, project_id, agent_id).status_code == 404


# ---------------- Adding a member ----------------

def test_an_agent_that_is_added_sees_the_project_and_its_key_from_the_user(client, app, alice):
    project_id, agent_id, agent = setup(client, alice)
    assert grant(client, alice, project_id, agent_id, "write", fill=5).status_code == 204

    projects = client.get("/v1/projects", headers=agent).json()
    assert [p["id"] for p in projects] == [project_id]
    assert projects[0]["role"] == "write"
    assert projects[0]["keys"] == [{"keyVersion": 1, "grantedBy": str(user_id(app, "alice")),
                                    "wrapped": wrapped(5)}]


def test_adding_again_changes_the_role_and_keeps_one_membership(client, alice):
    project_id, agent_id, agent = setup(client, alice)
    grant(client, alice, project_id, agent_id, "write")
    assert grant(client, alice, project_id, agent_id, "read", fill=6).status_code == 204

    projects = client.get("/v1/projects", headers=agent).json()
    assert len(projects) == 1
    assert projects[0]["role"] == "read"
    assert projects[0]["keys"][0]["wrapped"] == wrapped(6)
    assert len(client.get(f"/v1/projects/{project_id}/members", headers=alice).json()) == 2


def test_only_an_unrevoked_agent_of_the_owner_can_be_added(client, app, alice, bob):
    project_id = make_project(client, alice)
    others = make_agent(client, bob, "bobs")["agentId"]
    revoked = make_agent(client, alice, "gone")["agentId"]
    client.post(f"/v1/agents/{revoked}/revoke", headers=alice)

    assert grant(client, alice, project_id, others).status_code == 404
    assert grant(client, alice, project_id, revoked).status_code == 404
    assert grant(client, alice, project_id, new_id()).status_code == 404
    # In V1 only agents are members besides the owner.
    assert grant(client, alice, project_id, user_id(app, "bob")).status_code == 400


def test_the_keys_of_a_new_member_must_be_right(client, alice):
    project_id, agent_id, _ = setup(client, alice)
    path = f"/v1/projects/{project_id}/members/{agent_id}"

    def put(**body):
        return client.put(path, headers=alice, json={"role": "write", **body})

    key = {"keyVersion": 1, "wrapped": wrapped()}
    assert put(keys=[]).status_code == 400
    assert put(keys=[{"keyVersion": 2, "wrapped": wrapped()}]).status_code == 400  # not a version
    assert put(keys=[key, key]).status_code == 400  # twice
    assert put(keys=[{"keyVersion": 1, "wrapped": b64(bytes(91))}]).status_code == 400
    assert put(keys=[{"keyVersion": 1, "wrapped": "not base64url!"}]).status_code == 400
    assert put(keys=[key], extra=1).status_code == 400
    assert client.put(path, headers=alice, json={"role": "owner", "keys": [key]}).status_code == 400
    assert client.get("/v1/projects", headers=alice).json()[0]["id"] == project_id
    assert len(client.get(f"/v1/projects/{project_id}/members", headers=alice).json()) == 1


def test_the_newest_key_version_is_needed(client, alice):
    project_id, agent_id, _ = setup(client, alice)
    other = make_agent(client, alice, "other")["agentId"]
    grant(client, alice, project_id, other)
    assert revoke(client, alice, project_id, other, new_key(2, user_id_of(client, alice))).status_code == 204

    # The project is at version 2 now; a key of version 1 alone is not enough.
    assert grant(client, alice, project_id, agent_id, versions=(1,)).status_code == 400
    assert grant(client, alice, project_id, agent_id, versions=(1, 2)).status_code == 204


def user_id_of(client, headers):
    """The id of the user behind the headers, as the project list names it."""
    return client.get("/v1/projects", headers=headers).json()[0]["keys"][0]["grantedBy"]


# ---------------- The feed after a member joined ----------------

def test_a_new_member_makes_the_feed_show_the_project_again_with_the_same_revisions(client, alice):
    project_id, first, agent = setup(client, alice)
    grant(client, alice, project_id, first)
    task_id = make_task(client, alice, project_id)
    before = client.get("/v1/documents", headers=alice).json()
    cursor = before["nextCursor"]
    assert client.get(f"/v1/documents?cursor={cursor}", headers=alice).json()["items"] == []

    second = make_agent(client, alice, "second")
    grant(client, alice, project_id, second["agentId"])

    for headers in (alice, agent):
        again = client.get(f"/v1/documents?cursor={cursor}", headers=headers).json()
        assert {d["id"] for d in again["items"]} == {project_id, task_id}
        assert {d["rev"] for d in again["items"]} == {1}
        assert int(again["nextCursor"]) > int(cursor)
    # The newcomer gets everything from the start.
    newcomer = client.get("/v1/documents", headers=bearer(second["token"])).json()
    assert {d["id"] for d in newcomer["items"]} == {project_id, task_id}
    # And the order of the revisions is still the order of the feed.
    assert [d["id"] for d in before["items"]] == [project_id, task_id]


def test_changing_the_role_of_a_member_does_not_show_the_project_again(client, alice):
    project_id, agent_id, _ = setup(client, alice)
    grant(client, alice, project_id, agent_id)
    make_task(client, alice, project_id)
    cursor = client.get("/v1/head", headers=alice).json()["cursor"]

    grant(client, alice, project_id, agent_id, "read")

    assert client.get(f"/v1/documents?cursor={cursor}", headers=alice).json()["items"] == []


# ---------------- Revoking a member ----------------

def test_a_member_that_is_revoked_loses_the_project_at_once(client, alice):
    project_id, agent_id, agent = setup(client, alice)
    grant(client, alice, project_id, agent_id)
    task_id = make_task(client, alice, project_id)
    assert client.get(f"/v1/documents/{task_id}", headers=agent).status_code == 200

    assert revoke(client, alice, project_id, agent_id).status_code == 204

    assert client.get("/v1/projects", headers=agent).json() == []
    assert client.get(f"/v1/documents/{task_id}", headers=agent).status_code == 404
    assert write(client, agent, new_id(), task_body(project_id)).status_code == 404
    # The agent itself is not revoked: other projects may remain.
    assert client.get("/v1/head", headers=agent).status_code == 200
    assert revoke(client, alice, project_id, agent_id).status_code == 404


def test_the_owner_cannot_be_removed(client, app, alice):
    project_id = make_project(client, alice)
    assert revoke(client, alice, project_id, user_id(app, "alice")).status_code == 400


def test_a_new_key_version_is_given_to_the_remaining_members_only(client, app, alice):
    project_id, gone, gone_headers = setup(client, alice, "gone")
    stays = make_agent(client, alice, "stays")
    grant(client, alice, project_id, gone)
    grant(client, alice, project_id, stays["agentId"])
    owner = user_id(app, "alice")

    assert revoke(client, alice, project_id, gone,
                  new_key(2, owner, stays["agentId"])).status_code == 204

    mine = client.get("/v1/projects", headers=alice).json()[0]
    assert [k["keyVersion"] for k in mine["keys"]] == [1, 2]
    theirs = client.get("/v1/projects", headers=bearer(stays["token"])).json()[0]
    assert [k["keyVersion"] for k in theirs["keys"]] == [1, 2]
    assert theirs["keys"][1]["grantedBy"] == str(owner)
    assert client.get("/v1/projects", headers=gone_headers).json() == []
    # What is written afterwards may use the new version.
    assert write(client, alice, new_id(), task_body(project_id, keyVersion=2)).status_code == 200


def test_a_new_key_version_that_misses_a_member_or_names_a_stranger_changes_nothing(client, app, alice):
    project_id, gone, _ = setup(client, alice, "gone")
    stays = make_agent(client, alice, "stays")["agentId"]
    grant(client, alice, project_id, gone)
    grant(client, alice, project_id, stays)
    owner = user_id(app, "alice")

    wrong = [
        new_key(2, owner),  # misses an agent
        new_key(2, owner, stays, gone),  # the removed one
        new_key(2, owner, stays, new_id()),  # a stranger
        new_key(2, owner, stays, stays),  # twice
        new_key(3, owner, stays),  # not the next version
        new_key(1, owner, stays),  # not a new version
    ]
    for key in wrong:
        assert revoke(client, alice, project_id, gone, key).status_code == 400, key

    members = client.get(f"/v1/projects/{project_id}/members", headers=alice).json()
    assert len(members) == 3
    assert [k["keyVersion"] for k in client.get("/v1/projects", headers=alice).json()[0]["keys"]] == [1]


def test_the_wrapped_keys_of_a_new_version_must_have_the_right_size(client, app, alice):
    project_id, gone, _ = setup(client, alice, "gone")
    grant(client, alice, project_id, gone)
    key = {"keyVersion": 2, "members": [{"principalId": str(user_id(app, "alice")),
                                         "wrapped": b64(bytes(10))}]}

    assert revoke(client, alice, project_id, gone, key).status_code == 400
    assert len(client.get(f"/v1/projects/{project_id}/members", headers=alice).json()) == 2


def test_the_project_in_the_path_must_be_a_uuid(client, alice):
    assert client.get("/v1/projects/not-a-uuid/members", headers=alice).status_code == 400
