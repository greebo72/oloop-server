"""The settings of the server, read from the environment (prefix OLOOP_)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OLOOP_", env_file=".env", extra="ignore")

    # SQLAlchemy URL of the database. A compose setup points it at PostgreSQL.
    database_url: str = "sqlite:///./data/oloop.db"

    # Where the encrypted image blobs live as files (spec/SERVER.md, "Blobs").
    blob_dir: Path = Path("./data/blobs")

    # Addresses or networks (CIDR) of the reverse proxy whose X-Forwarded-For is
    # trusted, comma separated (spec/SERVER.md, "Deployment assumptions").
    # Empty trusts none.
    trusted_proxies: str = ""

    # Limits of spec/SERVER.md, "Limits".
    max_document_bytes: int = 256 * 1024
    max_blob_bytes: int = 10 * 1024 * 1024

    # How often unused blobs are removed (spec/SERVER.md, "Images"), in minutes.
    # 0 switches it off; `oloop-server gc-blobs` does it once.
    blob_gc_interval_minutes: float = 60

    # Tokens (spec/SERVER.md, "Authentication"). The spec fixes 15 minutes for
    # the access token; the life of a refresh token is renewed at every use.
    access_token_minutes: int = 15
    refresh_token_days: int = 30

    # Failed logins in the window that lock a user name or an address.
    login_window_minutes: int = 15
    login_max_failures_per_user: int = 5
    login_max_failures_per_address: int = 20


@lru_cache
def get_settings() -> Settings:
    return Settings()
