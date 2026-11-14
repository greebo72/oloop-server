"""The key bundle of the signed-in user (spec/SERVER.md, "Key bundle")."""
import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from oloop_server.auth import SessionDep, current_device
from oloop_server.db import utcnow
from oloop_server.encoding import b64url_decode, b64url_encode
from oloop_server.errors import ApiError
from oloop_server.models import Device, KeyBundle

router = APIRouter(prefix="/v1/keys")

MIB = 1024 * 1024

# What a bundle holds (spec/SERVER.md, "Encryption"): a 16 byte salt, a 32 byte
# public key, and the 32 byte secret key in an envelope: version, algorithm,
# 24 byte nonce, ciphertext, 16 byte tag.
SALT_BYTES = 16
PUBLIC_KEY_BYTES = 32
WRAPPED_SECRET_KEY_BYTES = 2 + 24 + 32 + 16

# The least cost a client accepts, and what libsodium takes at most.
MIN_M = 19 * MIB
MIN_T = 2
MAX_COST = 2**32 - 1

_REVISION = re.compile(r'^(?:W/)?"?(\d{1,9})"?$')


class BundleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int
    kdf: str
    salt: str
    m: int
    t: int
    publicKey: str
    wrappedSecretKey: str
    recoveryWrappedSecretKey: str


class PutKeysIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bundle: BundleIn


def _bad(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _binary(name: str, text: str, size: int) -> bytes:
    try:
        data = b64url_decode(text)
    except ValueError:
        raise _bad(f"{name} is not base64url without padding") from None
    if len(data) != size:
        raise _bad(f"{name} must be {size} bytes")
    return data


def _envelope(name: str, text: str) -> bytes:
    data = _binary(name, text, WRAPPED_SECRET_KEY_BYTES)
    if data[0] != 1 or data[1] != 1:
        raise _bad(f"{name} is not an envelope of version 1 with algorithm 1")
    return data


def parse_bundle(bundle: BundleIn) -> dict:
    """Checks the form of a bundle. The server cannot check the encryption."""
    if bundle.v != 1:
        raise _bad("v must be 1")
    if bundle.kdf != "argon2id":
        raise _bad('kdf must be "argon2id"')
    if not MIN_M <= bundle.m <= MAX_COST:
        raise _bad(f"m must be at least {MIN_M} (19 MiB) and at most {MAX_COST}")
    if not MIN_T <= bundle.t <= MAX_COST:
        raise _bad(f"t must be at least {MIN_T} and at most {MAX_COST}")

    return {
        "salt": _binary("salt", bundle.salt, SALT_BYTES),
        "m": bundle.m,
        "t": bundle.t,
        "public_key": _binary("publicKey", bundle.publicKey, PUBLIC_KEY_BYTES),
        "wrapped_secret_key": _envelope("wrappedSecretKey", bundle.wrappedSecretKey),
        "recovery_wrapped_secret_key": _envelope(
            "recoveryWrappedSecretKey", bundle.recoveryWrappedSecretKey),
    }


def _bundle_json(row: KeyBundle) -> dict:
    return {
        "v": 1,
        "kdf": "argon2id",
        "salt": b64url_encode(row.salt),
        "m": row.m,
        "t": row.t,
        "publicKey": b64url_encode(row.public_key),
        "wrappedSecretKey": b64url_encode(row.wrapped_secret_key),
        "recoveryWrappedSecretKey": b64url_encode(row.recovery_wrapped_secret_key),
    }


def _etag(rev: int) -> str:
    return f'"{rev}"'


DeviceDep = Annotated[Device, Depends(current_device)]


@router.get("")
def get_keys(device: DeviceDep, session: SessionDep, response: Response):
    row = session.get(KeyBundle, device.user_id)
    if row is None:
        raise ApiError(404, "not_found", "no key bundle has been set up yet")

    response.headers["ETag"] = _etag(row.rev)
    return {"keyRev": row.rev, "bundle": _bundle_json(row)}


@router.put("")
def put_keys(body: PutKeysIn, request: Request, response: Response,
             device: DeviceDep, session: SessionDep):
    values = parse_bundle(body.bundle)
    if_none_match = request.headers.get("if-none-match")
    if_match = request.headers.get("if-match")
    now: datetime = utcnow()

    if (if_none_match is None) == (if_match is None):
        raise _bad("exactly one of If-None-Match: * and If-Match is required")

    if if_none_match is not None:
        if if_none_match.strip() != "*":
            raise _bad("If-None-Match must be *")
        session.add(KeyBundle(user_id=device.user_id, rev=1, created_at=now,
                              updated_at=now, **values))
        try:
            session.commit()
        except IntegrityError:
            # Set up already, or by a request that was faster.
            session.rollback()
            raise ApiError(409, "conflict", "a key bundle exists already") from None
        response.headers["ETag"] = _etag(1)
        return {"keyRev": 1}

    match = _REVISION.match(if_match.strip())
    if match is None:
        raise _bad("If-Match must be the keyRev in quotes")
    expected = int(match.group(1))

    row = session.get(KeyBundle, device.user_id)
    if row is None:
        raise ApiError(404, "not_found", "no key bundle has been set up yet")
    if values["public_key"] != row.public_key:
        raise _bad("publicKey cannot change: it is the identity of the user")

    # In one step, so that of two updates with the same keyRev only one wins.
    changed = session.execute(
        update(KeyBundle)
        .where(KeyBundle.user_id == device.user_id, KeyBundle.rev == expected)
        .values(rev=expected + 1, updated_at=now, **values)
    ).rowcount
    if changed != 1:
        session.rollback()
        raise ApiError(409, "conflict", "the key bundle changed, load it again")

    session.commit()
    response.headers["ETag"] = _etag(expected + 1)
    return {"keyRev": expected + 1}
