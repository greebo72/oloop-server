import base64
import json
from pathlib import Path

import pytest

from conftest import bearer, login
from oloop_server.encoding import b64url_encode

VECTORS = Path(__file__).parent.parent / "spec" / "fixtures" / "server" / "vectors.json"


def vector_bundle():
    return json.loads(VECTORS.read_text())["keys"]["bundle"]


def b64(data: bytes) -> str:
    return b64url_encode(data)


def envelope(fill=1) -> str:
    return b64(bytes([1, 1]) + bytes([fill]) * 72)


def bundle(**changes):
    """A well-formed bundle; the content is not real, the server cannot tell."""
    base = {
        "v": 1, "kdf": "argon2id", "salt": b64(b"s" * 16), "m": 64 * 1024 * 1024, "t": 3,
        "publicKey": b64(b"p" * 32),
        "wrappedSecretKey": envelope(1), "recoveryWrappedSecretKey": envelope(2),
    }
    base.update(changes)
    return base


@pytest.fixture
def alice(client, make_user):
    make_user("alice")
    return bearer(login(client, "alice").json()["accessToken"])


def put(client, headers, body, **extra):
    return client.put("/v1/keys", json={"bundle": body}, headers={**headers, **extra})


def create(client, headers, body=None):
    return put(client, headers, body or bundle(), **{"If-None-Match": "*"})


# ---------------- Reading and first setup ----------------

def test_the_keys_need_a_login(client):
    assert client.get("/v1/keys").status_code == 401
    assert client.put("/v1/keys", json={"bundle": bundle()},
                      headers={"If-None-Match": "*"}).status_code == 401


def test_there_is_no_bundle_before_the_setup(client, alice):
    response = client.get("/v1/keys", headers=alice)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_the_first_setup_stores_the_bundle_and_returns_it_unchanged(client, alice):
    response = create(client, alice)

    assert response.status_code == 200
    assert response.json() == {"keyRev": 1}
    assert response.headers["ETag"] == '"1"'

    loaded = client.get("/v1/keys", headers=alice)
    assert loaded.json() == {"keyRev": 1, "bundle": bundle()}
    assert loaded.headers["ETag"] == '"1"'


def test_the_bundle_of_the_test_vectors_is_accepted_and_comes_back_the_same(client, alice):
    # The vectors were made by another implementation: this checks the sizes
    # and the spelling the server expects against theirs.
    given = vector_bundle()

    assert create(client, alice, given).status_code == 200

    assert client.get("/v1/keys", headers=alice).json()["bundle"] == given


def test_a_second_first_setup_is_a_conflict(client, alice):
    create(client, alice)

    response = create(client, alice, bundle(salt=b64(b"x" * 16)))

    assert response.status_code == 409
    assert client.get("/v1/keys", headers=alice).json()["bundle"] == bundle()


def test_every_user_has_a_bundle_of_their_own(client, make_user, alice):
    make_user("bob")
    bob = bearer(login(client, "bob").json()["accessToken"])
    create(client, alice)

    assert client.get("/v1/keys", headers=bob).status_code == 404
    assert create(client, bob, bundle(publicKey=b64(b"q" * 32))).status_code == 200
    assert client.get("/v1/keys", headers=alice).json()["bundle"]["publicKey"] == b64(b"p" * 32)


# ---------------- Changing it ----------------

def test_a_change_with_the_current_keyrev_raises_it(client, alice):
    create(client, alice)

    changed = bundle(wrappedSecretKey=envelope(7), salt=b64(b"n" * 16))
    response = put(client, alice, changed, **{"If-Match": '"1"'})

    assert response.status_code == 200
    assert response.json() == {"keyRev": 2}
    assert response.headers["ETag"] == '"2"'
    loaded = client.get("/v1/keys", headers=alice).json()
    assert loaded == {"keyRev": 2, "bundle": changed}


def test_a_change_with_an_old_keyrev_is_a_conflict(client, alice):
    create(client, alice)
    put(client, alice, bundle(salt=b64(b"a" * 16)), **{"If-Match": '"1"'})

    response = put(client, alice, bundle(salt=b64(b"b" * 16)), **{"If-Match": '"1"'})

    assert response.status_code == 409
    assert client.get("/v1/keys", headers=alice).json()["bundle"]["salt"] == b64(b"a" * 16)


def test_the_public_key_cannot_change(client, alice):
    create(client, alice)

    response = put(client, alice, bundle(publicKey=b64(b"z" * 32)), **{"If-Match": '"1"'})

    assert response.status_code == 400
    assert "publicKey" in response.json()["error"]["message"]
    assert client.get("/v1/keys", headers=alice).json()["keyRev"] == 1


def test_a_change_of_a_bundle_that_does_not_exist_is_not_found(client, alice):
    response = put(client, alice, bundle(), **{"If-Match": '"1"'})

    assert response.status_code == 404


def test_the_keyrev_is_taken_with_or_without_quotes(client, alice):
    create(client, alice)

    assert put(client, alice, bundle(), **{"If-Match": "1"}).json() == {"keyRev": 2}
    assert put(client, alice, bundle(), **{"If-Match": '"2"'}).json() == {"keyRev": 3}


# ---------------- The preconditions ----------------

def test_a_write_without_a_precondition_is_refused(client, alice):
    response = client.put("/v1/keys", json={"bundle": bundle()}, headers=alice)

    assert response.status_code == 400


def test_a_write_with_both_preconditions_is_refused(client, alice):
    response = put(client, alice, bundle(), **{"If-Match": '"1"', "If-None-Match": "*"})

    assert response.status_code == 400


@pytest.mark.parametrize("value", ["abc", '"1.5"', '"-1"', '""', "1 2"])
def test_a_keyrev_that_is_no_number_is_refused(client, alice, value):
    create(client, alice)

    assert put(client, alice, bundle(), **{"If-Match": value}).status_code == 400


def test_if_none_match_must_be_a_star(client, alice):
    assert put(client, alice, bundle(), **{"If-None-Match": '"1"'}).status_code == 400


# ---------------- The form of a bundle ----------------

@pytest.mark.parametrize("changes", [
    {"v": 2},
    {"kdf": "scrypt"},
    {"salt": b64(b"s" * 15)},
    {"salt": b64(b"s" * 17)},
    {"publicKey": b64(b"p" * 31)},
    {"wrappedSecretKey": b64(bytes([1, 1]) + b"x" * 71)},
    {"wrappedSecretKey": b64(bytes([2, 1]) + b"x" * 72)},
    {"wrappedSecretKey": b64(bytes([1, 2]) + b"x" * 72)},
    {"recoveryWrappedSecretKey": b64(bytes([1, 1]) + b"x" * 73)},
    {"recoveryWrappedSecretKey": b64(bytes([0, 1]) + b"x" * 72)},
    {"m": 19 * 1024 * 1024 - 1},
    {"t": 1},
    {"m": 2**32},
    {"t": 2**32},
    {"m": 10**30},
    {"salt": "not base64!"},
    {"salt": b64(b"s" * 16) + "="},
    {"salt": ""},
])
def test_a_bundle_that_is_wrong_in_form_is_refused(client, alice, changes):
    response = create(client, alice, bundle(**changes))

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    assert client.get("/v1/keys", headers=alice).status_code == 404


def test_the_least_cost_is_accepted(client, alice):
    assert create(client, alice, bundle(m=19 * 1024 * 1024, t=2)).status_code == 200


def test_the_largest_cost_is_accepted(client, alice):
    assert create(client, alice, bundle(m=2**32 - 1, t=2**32 - 1)).status_code == 200


def test_a_field_the_spec_does_not_know_is_refused(client, alice):
    response = create(client, alice, bundle(extra="x"))

    assert response.status_code == 400


def test_a_value_in_another_spelling_is_refused(client, alice):
    # 16 bytes are 22 characters = 132 bits, so the last 4 bits of the last
    # character are not used. Another value there decodes to the same bytes
    # but is another text.
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    text = b64(b"s" * 16)
    other = text[:-1] + alphabet[alphabet.index(text[-1]) | 1]
    assert other != text
    assert base64.urlsafe_b64decode(other + "==") == base64.urlsafe_b64decode(text + "==")

    assert create(client, alice, bundle(salt=other)).status_code == 400


def test_a_signed_out_device_cannot_read_the_bundle(client, alice):
    create(client, alice)
    client.post("/v1/auth/logout", headers=alice)

    assert client.get("/v1/keys", headers=alice).status_code == 401
