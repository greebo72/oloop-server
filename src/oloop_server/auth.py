"""Login, refresh and logout (spec/SERVER.md, "Authentication" and "API v1")."""
from datetime import datetime, timedelta
from math import ceil
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from oloop_server.config import Settings
from oloop_server.db import session_per_request, utcnow
from oloop_server.errors import ApiError
from oloop_server.models import Device, LoginFailure, Token, User
from oloop_server.netutil import client_address, parse_networks
from oloop_server.security import (
    hash_password,
    hash_token,
    needs_rehash,
    new_token,
    verify_password,
)

router = APIRouter(prefix="/v1/auth")

# One session per request. A dependency used twice in a request is the same one.
SessionDep = Annotated[Session, Depends(session_per_request)]

# How often the time of last use of a device is written.
_TOUCH_AFTER = timedelta(seconds=60)


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    # A limit on the length: hashing a huge password would cost a lot of time.
    password: str = Field(min_length=1, max_length=1024)
    deviceName: str = Field(min_length=1, max_length=100)


class RefreshIn(BaseModel):
    refreshToken: str = Field(min_length=1, max_length=200)


class TokensOut(BaseModel):
    accessToken: str
    expiresIn: int
    refreshToken: str


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _unauthorized(message: str) -> ApiError:
    return ApiError(401, "unauthorized", message, {"WWW-Authenticate": "Bearer"})


def username_key(username: str) -> str:
    return username.strip().casefold()


# ---------------- Limit on failed logins ----------------

def _check_limit(session: Session, key: str, address: str, now: datetime, settings: Settings):
    window = timedelta(minutes=settings.login_window_minutes)
    for column, value, limit in (
        (LoginFailure.username_key, key, settings.login_max_failures_per_user),
        (LoginFailure.address, address, settings.login_max_failures_per_address),
    ):
        times = session.scalars(
            select(LoginFailure.created_at)
            .where(column == value, LoginFailure.created_at > now - window)
            .order_by(LoginFailure.created_at)
        ).all()
        if len(times) >= limit:
            # The count falls below the limit when this failure leaves the window.
            wait = max(1, ceil((times[len(times) - limit] + window - now).total_seconds()))
            raise ApiError(
                429, "rate_limited", "too many failed logins, try again later",
                {"Retry-After": str(wait)},
            )


def _record_failure(session: Session, key: str, address: str, now: datetime, settings: Settings):
    window = timedelta(minutes=settings.login_window_minutes)
    session.add(LoginFailure(username_key=key[:100], address=address[:64], created_at=now))
    # Old failures are of no use any more.
    session.execute(delete(LoginFailure).where(LoginFailure.created_at < now - 2 * window))
    session.commit()


# ---------------- Tokens ----------------

def _issue(session: Session, device: Device, now: datetime, settings: Settings) -> TokensOut:
    access, access_hash = new_token("oloop_at")
    refresh, refresh_hash = new_token("oloop_rt")
    access_life = timedelta(minutes=settings.access_token_minutes)

    session.add(Token(device_id=device.id, kind="access", token_hash=access_hash,
                      created_at=now, expires_at=now + access_life))
    session.add(Token(device_id=device.id, kind="refresh", token_hash=refresh_hash,
                      created_at=now, expires_at=now + timedelta(days=settings.refresh_token_days)))
    return TokensOut(accessToken=access, expiresIn=int(access_life.total_seconds()),
                     refreshToken=refresh)


def _sign_out(session: Session, device: Device, now: datetime):
    device.revoked_at = now
    session.execute(delete(Token).where(Token.device_id == device.id))


# ---------------- The signed-in device ----------------

def current_device(request: Request, session: SessionDep) -> Device:
    """A dependency: the device behind the bearer token, or 401."""
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        raise _unauthorized("a bearer token is required")

    now = utcnow()
    token = session.scalar(
        select(Token).where(Token.token_hash == hash_token(value), Token.kind == "access")
    )
    if token is None or token.expires_at <= now or token.device.revoked_at is not None:
        raise _unauthorized("the token is not valid")

    device = token.device
    if device.last_used_at is None or now - device.last_used_at > _TOUCH_AFTER:
        device.last_used_at = now
        session.commit()
    return device


# ---------------- Endpoints ----------------

@router.post("/login")
def login(body: LoginIn, request: Request, session: SessionDep):
    settings = _settings(request)
    now = utcnow()
    key = username_key(body.username)
    address = client_address(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        parse_networks(settings.trusted_proxies),
    )

    _check_limit(session, key, address, now, settings)

    user = session.scalar(select(User).where(User.username_key == key))
    if not verify_password(user.password_hash if user else None, body.password) or user is None:
        _record_failure(session, key, address, now, settings)
        # The same answer for an unknown user and a wrong password.
        raise _unauthorized("wrong user name or password")

    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)

    device = Device(user_id=user.id, name=body.deviceName.strip() or "device", last_used_at=now)
    session.add(device)
    session.flush()
    tokens = _issue(session, device, now, settings)
    session.commit()
    return tokens


@router.post("/refresh")
def refresh(body: RefreshIn, request: Request, session: SessionDep):
    settings = _settings(request)
    now = utcnow()

    token = session.scalar(
        select(Token).where(Token.token_hash == hash_token(body.refreshToken),
                            Token.kind == "refresh")
    )
    if token is None or token.device.revoked_at is not None:
        raise _unauthorized("the token is not valid")
    device = token.device

    # Mark it used in one step: of two requests with the same token only one
    # gets here, the other one has shown a token that was used already.
    claimed = session.execute(
        update(Token).where(Token.id == token.id, Token.used_at.is_(None)).values(used_at=now)
    ).rowcount
    if claimed != 1:
        _sign_out(session, device, now)
        session.commit()
        raise _unauthorized("the token was used before, the device is signed out")

    if token.expires_at <= now:
        session.commit()
        raise _unauthorized("the token has expired")

    # What the device held before is replaced.
    session.execute(delete(Token).where(Token.device_id == device.id, Token.id != token.id))
    device.last_used_at = now
    tokens = _issue(session, device, now, settings)
    session.commit()
    return tokens


@router.post("/logout", status_code=204)
def logout(device: Annotated[Device, Depends(current_device)], session: SessionDep):
    _sign_out(session, device, utcnow())
    session.commit()
    return Response(status_code=204)
