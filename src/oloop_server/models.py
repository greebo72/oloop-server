"""The tables. What is stored is described in spec/SERVER.md; the login, the key
bundle, projects and documents are here so far."""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
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


class Counter(Base):
    """A counter. `changes` numbers every write of a document.

    A write takes the next number with an UPDATE, which holds the row until the
    write is committed. So numbers are committed in the order they were taken,
    and a client that has read up to a number has not skipped a smaller one.
    """

    __tablename__ = "counters"

    name: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger)


class Project(Base):
    __tablename__ = "projects"

    # The id of the project document.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ProjectMember(Base):
    """Who may use a project, with which role. The principal is a user or an
    agent; the id is not a foreign key because it points to either table."""

    __tablename__ = "project_members"
    __table_args__ = (
        CheckConstraint("kind IN ('user', 'agent')", name="kind"),
        CheckConstraint("role IN ('owner', 'write', 'read')", name="role"),
        Index("ix_project_members_principal_id", "principal_id"),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"), primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    role: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class ProjectKey(Base):
    """The key of a project, handed to one member (spec/SERVER.md, "Handing a
    project key to a principal"). The server cannot open it."""

    __tablename__ = "project_keys"
    __table_args__ = (
        ForeignKeyConstraint(
            ["project_id", "principal_id"],
            ["project_members.project_id", "project_members.principal_id"],
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    principal_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    key_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    granted_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    wrapped: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Document(Base):
    """A project or a task. Its content is in the revisions; this row says
    which revision is the current one and when it was written (`seq`)."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("kind IN ('project', 'task')", name="kind"),
        Index("ix_documents_project_id", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))
    # For a project document, its own id.
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    rev: Mapped[int] = mapped_column(Integer)
    seq: Mapped[int] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Revision(Base):
    """One state of a document, as the client wrote it. They are all kept."""

    __tablename__ = "document_revisions"

    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id"), primary_key=True)
    rev: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_version: Mapped[int] = mapped_column(Integer)
    blob_ref: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    size: Mapped[int] = mapped_column(Integer)
    # The principal that wrote it. Set by the server, never by the client.
    written_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    written_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    data: Mapped[bytes] = mapped_column(LargeBinary)
