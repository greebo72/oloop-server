"""The tables. What is stored is described in spec/SERVER.md; the login, the key
bundle, projects, documents, blobs and agents are here."""
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


class Blob(Base):
    """An encrypted image. The bytes are a file (see blobstore.py), this row is
    what is known about it. Created after the file is complete."""

    __tablename__ = "blobs"
    __table_args__ = (Index("ix_blobs_unreferenced_since", "unreferenced_since"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id"))
    # The task it was uploaded for. Only that task may refer to it.
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(Uuid)
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    # Null until a task refers to it.
    first_referenced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    # Since when no current revision refers to it. Null while one does.
    unreferenced_since: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class Agent(Base):
    """An agent of a user (spec/AGENTS.md). Never deleted, only revoked, so that
    `writtenBy` of its revisions stays meaningful."""

    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_owner_id", "owner_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(100))
    # Case-folded: the name is unique among the agents of a user that are not revoked.
    name_key: Mapped[str] = mapped_column(String(100))
    # The current key pair's public half (32 bytes).
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    # Made by the user's key when the agent was created (nonce and box). The server
    # cannot open it; clients check it.
    registration: Mapped[bytes] = mapped_column(LargeBinary)
    # How long a token is valid, counted from its issue (a renewal gives the same).
    lifetime_seconds: Mapped[int] = mapped_column(Integer)
    # No token is renewed to expire later than this.
    renew_until: Mapped[datetime] = mapped_column(UtcDateTime)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AgentToken(Base):
    """A bearer token of an agent. Only the SHA-256 of it is kept."""

    __tablename__ = "agent_tokens"
    __table_args__ = (Index("ix_agent_tokens_agent_id", "agent_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)
    # Set when a newer token was issued. A replaced token is valid a little longer,
    # so that a lost answer does not lock the agent out.
    replaced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)


class AgentKey(Base):
    """One link of the key chain of an agent: a key pair it rotated to, with the
    statement that the old key made for it. Number 1 is the key of the
    registration and has no row, so the first link is number 2."""

    __tablename__ = "agent_keys"

    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id"), primary_key=True)
    number: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    statement: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
