import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from conftest import bearer
from helpers import (
    add_agent_member,
    agent_body,
    b64,
    make_agent,
    make_project,
    make_task,
    new_id,
    project_body,
    task_body,
    user_id,
    write,
)
from oloop_server.models import Agent, AgentToken, ProjectKey, ProjectMember


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(days):
    return datetime.now(UTC) + timedelta(days=days)


# ---------------- Creating ----------------

def test_a_user_creates_an_agent_and_gets_its_token_once(client, alice):
    made = make_agent(client, alice)

    assert made["token"].startswith("oloop_ag_")
    listed = client.get("/v1/agents", headers=alice).json()
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["id"] == made["agentId"]
    assert listed[0]["name"] == "claude"
    assert listed[0]["keyChain"] == []
    assert listed[0]["revoked"] is False
    assert listed[0]["lastUsedAt"] is None


def test_the_defaults_are_ninety_days_for_a_token_and_a_year_for_the_renewal(client, alice):
    make_agent(client, alice)
    agent = client.get("/v1/agents", headers=alice).json()[0]

    created = datetime.strptime(agent["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    expires = datetime.strptime(agent["expiresAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    renew = datetime.strptime(agent["renewUntil"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((expires - created).days - 90) <= 1
    assert abs((renew - created).days - 365) <= 1


def test_the_expiry_and_the_renewal_period_can_be_asked_for(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(20)))
    agent = client.get("/v1/agents", headers=alice).json()[0]

    assert made["expiresAt"] == agent["expiresAt"]
    assert agent["expiresAt"][:10] == stamp(at(10))[:10]
    assert agent["renewUntil"][:10] == stamp(at(20))[:10]


def test_a_renewal_period_shorter_than_the_token_is_refused(client, alice):
    response = client.post("/v1/agents", headers=alice, json=agent_body(
        expiresAt=stamp(at(30)), renewUntil=stamp(at(10))))
    assert response.status_code == 400


def test_an_expiry_in_the_past_or_beyond_ten_years_is_refused(client, alice):
    for days in (-1, 365 * 11):
        response = client.post("/v1/agents", headers=alice, json=agent_body(expiresAt=stamp(at(days))))
        assert response.status_code == 400, days


def test_a_time_without_a_zone_is_refused(client, alice):
    response = client.post("/v1/agents", headers=alice, json=agent_body(expiresAt="2030-01-01T00:00:00"))
    assert response.status_code == 400


def test_the_keys_must_have_the_right_size_and_form(client, alice):
    for changes in (
        {"publicKey": b64(bytes(31))},
        {"publicKey": "not base64url!"},
        {"registration": b64(bytes(87))},
        {"registration": b64(bytes(89))},
        {"name": "   "},
        {"name": "x" * 101},
        {"unknown": 1},
    ):
        response = client.post("/v1/agents", headers=alice, json=agent_body(**changes))
        assert response.status_code == 400, changes


def test_the_agent_gets_the_id_the_client_chose(client, alice):
    chosen = new_id()
    made = make_agent(client, alice, id=chosen)
    assert made["agentId"] == chosen
    assert [row["id"] for row in client.get("/v1/agents", headers=alice).json()] == [chosen]


def test_an_id_in_capitals_is_stored_and_answered_in_lower_case(client, alice):
    chosen = new_id()
    made = make_agent(client, alice, id=chosen.upper())
    assert made["agentId"] == chosen


def test_the_id_is_required_and_must_be_a_version_4_uuid(client, alice):
    body = agent_body()
    del body["id"]
    assert client.post("/v1/agents", headers=alice, json=body).status_code == 400
    for bad in ("not-a-uuid", "", 5, None, str(uuid.uuid1()), str(uuid.uuid5(uuid.NAMESPACE_DNS, "x"))):
        response = client.post("/v1/agents", headers=alice, json=agent_body(id=bad))
        assert response.status_code == 400, bad


def test_an_id_that_exists_is_a_conflict_whoever_owns_it(client, alice, bob):
    chosen = new_id()
    make_agent(client, alice, "first", id=chosen)

    for headers, name in ((alice, "other"), (bob, "first")):
        response = client.post("/v1/agents", headers=headers, json=agent_body(name, id=chosen))
        assert response.status_code == 409
    # Nothing was written by the refused requests: the owner still has one agent, bob none.
    assert len(client.get("/v1/agents", headers=alice).json()) == 1
    assert client.get("/v1/agents", headers=bob).json() == []


def test_an_id_stays_taken_after_a_revoke(client, alice):
    chosen = new_id()
    made = make_agent(client, alice, id=chosen)
    assert client.post(f"/v1/agents/{made['agentId']}/revoke", headers=alice).status_code == 204
    assert client.post("/v1/agents", headers=alice, json=agent_body("again", id=chosen)).status_code == 409


def test_a_name_is_taken_once_without_regard_to_case_but_free_again_after_a_revoke(client, alice):
    first = make_agent(client, alice, "Claude")

    again = client.post("/v1/agents", headers=alice, json=agent_body("  claude "))
    assert again.status_code == 409

    assert client.post(f"/v1/agents/{first['agentId']}/revoke", headers=alice).status_code == 204
    assert client.post("/v1/agents", headers=alice, json=agent_body("claude")).status_code == 200


def test_two_users_may_use_the_same_agent_name(client, alice, bob):
    make_agent(client, alice)
    make_agent(client, bob)


def test_only_the_hash_of_the_token_is_stored(client, alice, db):
    made = make_agent(client, alice)
    assert db.scalars(select(AgentToken.token_hash)).all() != [made["token"]]
    assert made["token"] not in db.scalars(select(AgentToken.token_hash)).all()


def test_the_registration_and_key_are_returned_as_stored(client, alice):
    make_agent(client, alice)
    agent = client.get("/v1/agents", headers=alice).json()[0]
    assert agent["publicKey"] == b64(bytes([7]) * 32)
    assert agent["registration"] == b64(bytes([8]) * 88)


# ---------------- Listing and ownership ----------------

def test_a_user_sees_only_their_own_agents(client, alice, bob):
    make_agent(client, alice, "claude")
    make_agent(client, bob, "other")

    assert [a["name"] for a in client.get("/v1/agents", headers=alice).json()] == ["claude"]
    assert [a["name"] for a in client.get("/v1/agents", headers=bob).json()] == ["other"]


def test_the_endpoints_need_a_sign_in(client):
    assert client.get("/v1/agents").status_code == 401
    assert client.post("/v1/agents", json=agent_body()).status_code == 401


def test_a_user_cannot_revoke_or_renew_the_agent_of_another(client, alice, bob):
    made = make_agent(client, alice)
    for path in ("revoke", "token"):
        assert client.post(f"/v1/agents/{made['agentId']}/{path}", headers=bob).status_code == 404


# ---------------- Revoking and a new token ----------------

def test_revoking_is_final_and_can_be_repeated(client, alice):
    made = make_agent(client, alice)
    path = f"/v1/agents/{made['agentId']}"

    assert client.post(f"{path}/revoke", headers=alice).status_code == 204
    assert client.post(f"{path}/revoke", headers=alice).status_code == 204
    assert client.get("/v1/agents", headers=alice).json()[0]["revoked"] is True
    assert client.post(f"{path}/token", headers=alice).status_code == 409


def test_a_new_token_ends_the_old_one_and_starts_a_new_renewal_period(client, alice, db):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(40)))

    # The renewal period ran for 40 days; move it into the past to see it start anew.
    agent = db.scalar(select(Agent))
    agent.renew_until = datetime.now(UTC) + timedelta(days=1)
    db.commit()

    new = client.post(f"/v1/agents/{made['agentId']}/token", headers=alice).json()

    assert new["token"] != made["token"]
    assert new["token"].startswith("oloop_ag_")
    db.expire_all()
    renew_until = db.scalar(select(Agent)).renew_until
    assert (renew_until - datetime.now(UTC)).days >= 39


# ---------------- Signing in as an agent ----------------

def test_an_agent_token_works_as_a_bearer_token_and_marks_the_use(client, alice):
    made = make_agent(client, alice)
    agent = bearer(made["token"])

    assert client.get("/v1/head", headers=agent).status_code == 200
    assert client.get("/v1/projects", headers=agent).json() == []
    assert client.get("/v1/documents", headers=agent).json()["items"] == []
    assert client.get("/v1/agents", headers=alice).json()[0]["lastUsedAt"] is not None


def test_a_wrong_expired_or_revoked_token_is_refused(client, alice, db):
    made = make_agent(client, alice)
    assert client.get("/v1/head", headers=bearer("oloop_ag_nope")).status_code == 401

    row = db.scalar(select(AgentToken))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get("/v1/head", headers=bearer(made["token"])).status_code == 401

    other = make_agent(client, alice, "other")
    assert client.get("/v1/head", headers=bearer(other["token"])).status_code == 200
    client.post(f"/v1/agents/{other['agentId']}/revoke", headers=alice)
    assert client.get("/v1/head", headers=bearer(other["token"])).status_code == 401


def test_a_new_token_given_by_the_user_ends_the_old_one_at_once(client, alice):
    made = make_agent(client, alice)
    new = client.post(f"/v1/agents/{made['agentId']}/token", headers=alice).json()

    assert client.get("/v1/head", headers=bearer(made["token"])).status_code == 401
    assert client.get("/v1/head", headers=bearer(new["token"])).status_code == 200


def test_an_agent_cannot_do_what_is_for_users(client, alice):
    agent = bearer(make_agent(client, alice)["token"])
    some_id = new_id()

    assert write(client, agent, some_id, project_body()).status_code == 403
    assert client.get("/v1/keys", headers=agent).status_code == 403
    assert client.put("/v1/keys", headers=agent, json={}).status_code == 403
    assert client.post("/v1/auth/logout", headers=agent).status_code == 403
    assert client.get("/v1/agents", headers=agent).status_code == 403
    assert client.post("/v1/agents", headers=agent, json=agent_body("x")).status_code == 403
    assert client.post(f"/v1/agents/{some_id}/revoke", headers=agent).status_code == 403
    assert client.post(f"/v1/agents/{some_id}/token", headers=agent).status_code == 403
    # Nothing was made on the way.
    assert client.get("/v1/projects", headers=alice).json() == []


def test_an_agent_sees_nothing_of_a_project_it_was_not_given(client, app, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    agent = bearer(make_agent(client, alice)["token"])

    assert client.get("/v1/projects", headers=agent).json() == []
    assert client.get("/v1/documents", headers=agent).json()["items"] == []
    assert client.get(f"/v1/documents/{task_id}", headers=agent).status_code == 404
    assert write(client, agent, new_id(), task_body(project_id)).status_code == 404
    assert write(client, agent, task_id, task_body(project_id, rev=2), base=1).status_code == 404


def test_an_agent_with_write_reads_and_writes_tasks_and_is_named_as_the_writer(client, app, alice):
    project_id = make_project(client, alice)
    made = make_agent(client, alice)
    agent = bearer(made["token"])
    add_agent_member(app, project_id, made["agentId"], "write")

    assert [p["id"] for p in client.get("/v1/projects", headers=agent).json()] == [project_id]
    task_id = new_id()
    assert write(client, agent, task_id, task_body(project_id)).status_code == 200
    assert write(client, agent, task_id, task_body(project_id, rev=2, fill=2), base=1).status_code == 200

    document = client.get(f"/v1/documents/{task_id}", headers=alice).json()
    assert document["writtenBy"] == made["agentId"]
    assert document["writtenBy"] != str(user_id(app, "alice"))


def test_an_agent_with_read_only_reads(client, app, alice):
    project_id = make_project(client, alice)
    task_id = make_task(client, alice, project_id)
    made = make_agent(client, alice)
    agent = bearer(made["token"])
    add_agent_member(app, project_id, made["agentId"], "read")

    assert client.get(f"/v1/documents/{task_id}", headers=agent).status_code == 200
    assert write(client, agent, new_id(), task_body(project_id)).status_code == 403
    assert write(client, agent, task_id, task_body(project_id, rev=2), base=1).status_code == 403


def test_revoking_an_agent_removes_its_memberships_and_keys(client, app, alice, db):
    project_id = make_project(client, alice)
    made = make_agent(client, alice)
    add_agent_member(app, project_id, made["agentId"], "write")

    client.post(f"/v1/agents/{made['agentId']}/revoke", headers=alice)

    assert db.scalars(select(ProjectMember.principal_id)).all() == [user_id(app, "alice")]
    assert db.scalars(select(ProjectKey.principal_id)).all() == [user_id(app, "alice")]
    assert client.get("/v1/head", headers=bearer(made["token"])).status_code == 401
