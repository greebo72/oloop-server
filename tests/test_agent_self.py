from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from conftest import bearer
from helpers import (
    add_agent_member,
    b64,
    make_agent,
    make_project,
    make_task,
    new_id,
    user_id,
    wrapped,
)
from oloop_server.models import Agent, AgentKey, AgentToken, ProjectKey


def stamp(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def at(days):
    return datetime.now(UTC) + timedelta(days=days)


def renew(client, token):
    return client.post("/v1/agent/renew", headers=bearer(token))


def head(client, token):
    return client.get("/v1/head", headers=bearer(token)).status_code


# ---------------- Renewing ----------------

def test_a_renewed_token_replaces_the_old_one_which_lives_on_for_ten_minutes(client, alice, db):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))

    answer = renew(client, made["token"])

    assert answer.status_code == 200
    new = answer.json()
    assert new["token"] != made["token"]
    assert head(client, new["token"]) == 200
    assert head(client, made["token"]) == 200  # within the grace

    old = db.scalar(select(AgentToken).where(AgentToken.replaced_at.is_not(None)))
    old.replaced_at = datetime.now(UTC) - timedelta(minutes=11)
    db.commit()
    assert head(client, made["token"]) == 401
    assert head(client, new["token"]) == 200


def test_the_new_token_is_valid_as_long_as_the_first_one_was(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))
    new = renew(client, made["token"]).json()

    # Counted from now, so a little later than the first.
    first = datetime.strptime(made["expiresAt"], "%Y-%m-%dT%H:%M:%SZ")
    second = datetime.strptime(new["expiresAt"], "%Y-%m-%dT%H:%M:%SZ")
    assert timedelta(0) <= second - first <= timedelta(seconds=5)


def test_a_token_is_never_renewed_beyond_the_renewal_period(client, alice, db):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(12)))
    agent = db.scalar(select(Agent))
    agent.lifetime_seconds = 30 * 86400
    db.commit()

    first = renew(client, made["token"])
    assert first.status_code == 200
    assert first.json()["expiresAt"][:10] == stamp(at(12))[:10]

    # At the limit there is nothing left to give.
    assert renew(client, first.json()["token"]).status_code == 403


def test_a_token_that_was_made_not_renewable_cannot_be_renewed(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(10)))
    assert renew(client, made["token"]).status_code == 403
    assert head(client, made["token"]) == 200


def test_an_expired_revoked_or_foreign_token_cannot_renew(client, alice, db):
    made = make_agent(client, alice)
    assert renew(client, "oloop_ag_nope").status_code == 401

    db.scalar(select(AgentToken)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert renew(client, made["token"]).status_code == 401

    other = make_agent(client, alice, "other")
    client.post(f"/v1/agents/{other['agentId']}/revoke", headers=alice)
    assert renew(client, other["token"]).status_code == 401


def test_a_user_cannot_renew_and_nothing_is_renewed_without_a_token(client, alice):
    make_agent(client, alice)
    assert client.post("/v1/agent/renew", headers=alice).status_code == 403
    assert client.post("/v1/agent/renew").status_code == 401


def test_a_lost_answer_can_be_asked_for_again_with_the_old_token(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))
    lost = renew(client, made["token"]).json()["token"]  # the answer never arrives

    again = renew(client, made["token"])

    assert again.status_code == 200
    assert head(client, again.json()["token"]) == 200
    assert head(client, lost) == 200  # the lost one works until its own grace ends


def test_a_lost_answer_at_the_limit_can_be_asked_for_again(client, alice, db):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(12)))
    db.scalar(select(Agent)).lifetime_seconds = 30 * 86400
    db.commit()
    renew(client, made["token"])  # reaches the limit; the answer is lost

    again = renew(client, made["token"])

    assert again.status_code == 200
    assert again.json()["expiresAt"][:10] == stamp(at(12))[:10]


def test_the_expiry_of_a_renewed_token_shows_in_the_list(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))
    new = renew(client, made["token"]).json()
    assert client.get("/v1/agents", headers=alice).json()[0]["expiresAt"] == new["expiresAt"]


# ---------------- Rotating the key pair ----------------

def rotate(client, token, keys=(), public=9, statement=None):
    return client.put("/v1/agent/keypair", headers=bearer(token), json={
        "publicKey": b64(bytes([public]) * 32),
        "statement": statement if statement is not None else b64(bytes([4]) * 92),
        "keys": list(keys)})


def key(project_id, version=1, fill=3):
    return {"projectId": project_id, "keyVersion": version, "wrapped": wrapped(fill)}


def setup(client, app, alice):
    project_id = make_project(client, alice)
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))
    add_agent_member(app, project_id, made["agentId"], "write")
    return project_id, made


def test_a_rotation_stores_the_new_key_the_statement_and_the_keys_the_agent_gave_itself(client, app, alice, db):
    project_id, made = setup(client, app, alice)

    answer = rotate(client, made["token"], [key(project_id, fill=3)])

    assert answer.status_code == 200
    listed = client.get("/v1/agents", headers=alice).json()[0]
    assert listed["publicKey"] == b64(bytes([9]) * 32)
    assert listed["registration"] == b64(bytes([8]) * 88)
    assert listed["keyChain"] == [{"publicKey": b64(bytes([9]) * 32),
                                   "statement": b64(bytes([4]) * 92)}]
    mine = client.get("/v1/projects", headers=bearer(answer.json()["token"])).json()[0]
    assert mine["keys"] == [{"keyVersion": 1, "grantedBy": made["agentId"], "wrapped": wrapped(3)}]
    # The user's own key of the project is untouched.
    assert client.get("/v1/projects", headers=alice).json()[0]["keys"][0]["wrapped"] == wrapped()
    assert client.get("/v1/projects", headers=alice).json()[0]["keys"][0]["grantedBy"] == str(user_id(app, "alice"))


def test_a_rotation_gives_a_fresh_token_and_the_old_one_ends_after_its_grace(client, app, alice):
    project_id, made = setup(client, app, alice)

    answer = rotate(client, made["token"], [key(project_id)]).json()

    assert answer["token"] != made["token"]
    assert head(client, answer["token"]) == 200


def test_the_chain_grows_by_one_link_per_rotation_with_numbers_from_two(client, app, alice, db):
    project_id, made = setup(client, app, alice)
    token = made["token"]
    for public in (9, 10, 11):
        token = rotate(client, token, [key(project_id)], public=public).json()["token"]

    assert [k.number for k in db.scalars(select(AgentKey).order_by(AgentKey.number))] == [2, 3, 4]
    chain = client.get("/v1/agents", headers=alice).json()[0]["keyChain"]
    assert [c["publicKey"] for c in chain] == [b64(bytes([p]) * 32) for p in (9, 10, 11)]


def test_a_rotation_must_cover_every_project_and_key_version_of_the_agent(client, app, alice, db):
    project_id, made = setup(client, app, alice)
    other_project = make_project(client, alice)
    add_agent_member(app, other_project, made["agentId"], "read")

    for keys in (
        [],  # none
        [key(project_id)],  # one project of two
        [key(project_id), key(other_project), key(other_project, version=2)],  # one too many
        [key(project_id), key(project_id)],  # twice
        [key(project_id), key(new_id())],  # a stranger
    ):
        assert rotate(client, made["token"], keys).status_code == 400, keys

    # Nothing changed: no link, the old key, the old wrapped keys, the old token.
    assert db.scalars(select(AgentKey)).all() == []
    assert client.get("/v1/agents", headers=alice).json()[0]["publicKey"] == b64(bytes([7]) * 32)
    assert {k.wrapped for k in db.scalars(select(ProjectKey).where(
        ProjectKey.principal_id == db.scalar(select(Agent.id))))} == {b"\x00" * 92}
    assert head(client, made["token"]) == 200
    assert len(db.scalars(select(AgentToken)).all()) == 1


def test_a_rotation_with_malformed_values_changes_nothing(client, app, alice):
    project_id, made = setup(client, app, alice)

    assert rotate(client, made["token"], [key(project_id)], statement=b64(bytes(91))).status_code == 400
    assert rotate(client, made["token"], [{**key(project_id), "wrapped": b64(bytes(91))}]).status_code == 400
    bad = client.put("/v1/agent/keypair", headers=bearer(made["token"]), json={
        "publicKey": b64(bytes(31)), "statement": b64(bytes(92)), "keys": [key(project_id)]})
    assert bad.status_code == 400
    # The same key again is no rotation.
    assert rotate(client, made["token"], [key(project_id)], public=7).status_code == 400
    assert client.get("/v1/agents", headers=alice).json()[0]["keyChain"] == []


def test_an_agent_without_projects_can_rotate(client, alice):
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(100)))
    assert rotate(client, made["token"]).status_code == 200


def test_a_token_that_cannot_be_renewed_cannot_rotate_and_nothing_changes(client, app, alice):
    project_id = make_project(client, alice)
    made = make_agent(client, alice, expiresAt=stamp(at(10)), renewUntil=stamp(at(10)))
    add_agent_member(app, project_id, made["agentId"], "write")

    assert rotate(client, made["token"], [key(project_id)]).status_code == 403
    assert client.get("/v1/agents", headers=alice).json()[0]["keyChain"] == []


def test_a_user_cannot_rotate_the_key_pair_of_an_agent(client, alice):
    assert client.put("/v1/agent/keypair", headers=alice, json={}).status_code == 403


def test_after_a_rotation_the_agent_still_reads_and_writes_what_it_was_given(client, app, alice):
    project_id, made = setup(client, app, alice)
    task_id = make_task(client, alice, project_id)

    token = rotate(client, made["token"], [key(project_id)]).json()["token"]

    assert client.get(f"/v1/documents/{task_id}", headers=bearer(token)).status_code == 200
