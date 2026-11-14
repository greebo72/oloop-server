"""The settings of the server, read from the environment (prefix OLOOP_)."""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OLOOP_", env_file=".env", extra="ignore")

    # SQLAlchemy URL of the database. The compose file points it at PostgreSQL.
    database_url: str = "sqlite:///./data/oloop.db"

    # Where the encrypted image blobs live as files (spec/SERVER.md, "Blobs").
    blob_dir: Path = Path("./data/blobs")

    # Addresses of the reverse proxy whose X-Forwarded-* headers are trusted
    # (spec/SERVER.md, "Deployment assumptions"). Empty trusts none.
    trusted_proxies: str = ""

    # Limits of spec/SERVER.md, "Limits".
    max_document_bytes: int = 256 * 1024
    max_blob_bytes: int = 10 * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
