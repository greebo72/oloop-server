"""Passwords and tokens."""
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id

# Checked when the user does not exist, so an unknown name takes as long as a
# wrong password.
_DUMMY_HASH = _hasher.hash("a password nobody has")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True when the password fits. `stored_hash` is None for an unknown user."""
    try:
        fits = _hasher.verify(stored_hash if stored_hash is not None else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        return False
    return fits and stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


def new_token(prefix: str) -> tuple[str, str]:
    """A new random token and the hash that is stored for it."""
    token = f"{prefix}_{secrets.token_urlsafe(32)}"
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
