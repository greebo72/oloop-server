from datetime import timedelta

from sqlalchemy import select

from conftest import PASSWORD, bearer, login
from oloop_server.db import utcnow
from oloop_server.models import Device, LoginFailure, Token

# ---------------- Login ----------------

def test_login_returns_two_tokens_and_the_life_of_the_access_token(client, make_user):
    make_user()

    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["expiresIn"] == 15 * 60
    assert body["accessToken"].startswith("oloop_at_")
    assert body["refreshToken"].startswith("oloop_rt_")
    assert body["accessToken"] != body["refreshToken"]


def test_tokens_are_stored_only_as_hashes(client, make_user, db):
    make_user()
    body = login(client).json()

    stored = {t.token_hash for t in db.scalars(select(Token))}

    assert len(stored) == 2
    assert body["accessToken"] not in stored and body["refreshToken"] not in stored
    assert all(len(h) == 64 for h in stored)


def test_the_user_name_is_not_case_sensitive(client, make_user):
    make_user("Alice")

    assert login(client, "alice").status_code == 200
    assert login(client, "ALICE").status_code == 200


def test_a_wrong_password_and_an_unknown_user_get_the_same_answer(client, make_user):
    make_user()

    wrong = login(client, password="not the password")
    unknown = login(client, username="nobody")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_a_huge_password_is_refused_before_it_is_hashed(client, make_user):
    make_user()

    response = login(client, password="x" * 1025)

    assert response.status_code == 400


def test_every_login_is_a_device_of_its_own(client, make_user, db):
    make_user()

    login(client, device="laptop")
    login(client, device="phone")

    assert sorted(d.name for d in db.scalars(select(Device))) == ["laptop", "phone"]


# ---------------- Logout and the access token ----------------

def test_logout_needs_a_token(client):
    response = client.post("/v1/auth/logout")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_logout_ends_both_tokens_of_the_device_only(client, make_user):
    make_user()
    first = login(client, device="laptop").json()
    second = login(client, device="phone").json()

    assert client.post("/v1/auth/logout", headers=bearer(first["accessToken"])).status_code == 204

    assert client.post("/v1/auth/logout", headers=bearer(first["accessToken"])).status_code == 401
    assert client.post("/v1/auth/refresh",
                       json={"refreshToken": first["refreshToken"]}).status_code == 401
    # the other device is not touched
    assert client.post("/v1/auth/refresh",
                       json={"refreshToken": second["refreshToken"]}).status_code == 200


def test_an_expired_access_token_is_refused(client, make_user, db):
    make_user()
    body = login(client).json()
    token = db.scalar(select(Token).where(Token.kind == "access"))
    token.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    response = client.post("/v1/auth/logout", headers=bearer(body["accessToken"]))

    assert response.status_code == 401


def test_a_refresh_token_is_no_access_token(client, make_user):
    make_user()
    body = login(client).json()

    response = client.post("/v1/auth/logout", headers=bearer(body["refreshToken"]))

    assert response.status_code == 401


def test_a_token_that_is_not_a_bearer_is_refused(client, make_user):
    make_user()
    body = login(client).json()

    response = client.post("/v1/auth/logout",
                           headers={"Authorization": f"Basic {body['accessToken']}"})

    assert response.status_code == 401


# ---------------- Refresh ----------------

def test_refresh_gives_new_tokens_and_ends_the_old_access_token(client, make_user):
    make_user()
    old = login(client).json()

    response = client.post("/v1/auth/refresh", json={"refreshToken": old["refreshToken"]})

    assert response.status_code == 200
    new = response.json()
    assert new["accessToken"] != old["accessToken"]
    assert new["refreshToken"] != old["refreshToken"]
    assert client.post("/v1/auth/logout", headers=bearer(old["accessToken"])).status_code == 401
    assert client.post("/v1/auth/logout", headers=bearer(new["accessToken"])).status_code == 204


def test_a_refresh_token_used_twice_signs_the_device_out(client, make_user):
    make_user()
    old = login(client).json()
    new = client.post("/v1/auth/refresh", json={"refreshToken": old["refreshToken"]}).json()

    again = client.post("/v1/auth/refresh", json={"refreshToken": old["refreshToken"]})

    assert again.status_code == 401
    # the newer tokens of the device are dead, too
    assert client.post("/v1/auth/refresh",
                       json={"refreshToken": new["refreshToken"]}).status_code == 401
    assert client.post("/v1/auth/logout", headers=bearer(new["accessToken"])).status_code == 401


def test_an_expired_refresh_token_is_refused(client, make_user, db):
    make_user()
    body = login(client).json()
    token = db.scalar(select(Token).where(Token.kind == "refresh"))
    token.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    response = client.post("/v1/auth/refresh", json={"refreshToken": body["refreshToken"]})

    assert response.status_code == 401


def test_an_unknown_refresh_token_is_refused(client):
    response = client.post("/v1/auth/refresh", json={"refreshToken": "oloop_rt_nonsense"})

    assert response.status_code == 401


# ---------------- The limit on failed logins ----------------

def test_the_sixth_failed_login_is_locked_and_says_when_to_come_back(client, make_user):
    make_user()
    for _ in range(5):
        assert login(client, password="wrong").status_code == 401

    response = login(client, password="wrong")

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert 0 < int(response.headers["Retry-After"]) <= 15 * 60


def test_the_lock_holds_for_the_right_password_too(client, make_user):
    make_user()
    for _ in range(5):
        login(client, password="wrong")

    assert login(client, password=PASSWORD).status_code == 429


def test_the_lock_of_one_user_does_not_lock_another(client, make_user):
    make_user("alice")
    make_user("bob")
    for _ in range(5):
        login(client, "alice", password="wrong")

    assert login(client, "alice").status_code == 429
    assert login(client, "bob").status_code == 200


def test_failures_outside_the_window_do_not_count(client, make_user, db):
    make_user()
    for _ in range(5):
        login(client, password="wrong")
    for failure in db.scalars(select(LoginFailure)):
        failure.created_at = utcnow() - timedelta(minutes=16)
    db.commit()

    assert login(client).status_code == 200


def test_one_address_is_locked_after_too_many_failures_for_different_users(client, db):
    for number in range(20):
        assert login(client, f"user{number}", password="wrong").status_code == 401

    response = login(client, "one-more", password="wrong")

    assert response.status_code == 429


def test_an_unknown_user_counts_too(client, db):
    for _ in range(5):
        login(client, "nobody", password="wrong")

    assert login(client, "nobody", password="wrong").status_code == 429
    assert len(db.scalars(select(LoginFailure)).all()) == 5
