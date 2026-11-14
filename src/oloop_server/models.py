"""The tables. What is stored is described in spec/SERVER.md; the login and the
key bundle are here so far."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from oloop_server.db import Base, UtcDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # As typed when the account was created.
    username: Mapped[str] = mapped_column(String(100))
    # Case-folded, so "Alice" and "alice" are one account.
    username_key: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Device(Base):
    """One sign-in. Its tokens end together when it is signed out."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    user: Mapped[User] = relationship()


class Token(Base):
    """An access or a refresh token. Only the SHA-256 of it is kept."""

    __tablename__ = "tokens"
    __table_args__ = (CheckConstraint("kind IN ('access', 'refresh')", name="kind"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"))
    kind: Mapped[str] = mapped_column(String(10))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # A refresh token is used once. Showing it again signs the device out.
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    device: Mapped[Device] = relationship()


class LoginFailure(Base):
    """A failed login, for the limit on user names and addresses."""

    __tablename__ = "login_failures"
    __table_args__ = (
        Index("ix_login_failures_username_key_created_at", "username_key", "created_at"),
        Index("ix_login_failures_address_created_at", "address", "created_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    username_key: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class KeyBundle(Base):
    """The key bundle of a user (spec/SERVER.md, "Key bundle"): the key pair,
    with the secret key encrypted twice. The server cannot open it."""

    __tablename__ = "key_bundles"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    # Starts at 1, rises with every change; the precondition of an update.
    rev: Mapped[int] = mapped_column(Integer)
    salt: Mapped[bytes] = mapped_column(LargeBinary)
    m: Mapped[int] = mapped_column(BigInteger)
    t: Mapped[int] = mapped_column(BigInteger)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    wrapped_secret_key: Mapped[bytes] = mapped_column(LargeBinary)
    recovery_wrapped_secret_key: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
