from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from helpers import agent_body, b64, make_agent
from oloop_server.models import Agent, AgentToken


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
