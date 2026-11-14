"""The application. Behind a reverse proxy that ends TLS; this speaks plain HTTP."""
from fastapi import FastAPI

from oloop_server import __version__

app = FastAPI(
    title="oloop-server",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Alive. No data and no authentication (spec/SERVER.md, "API v1")."""
    return {"status": "ok"}
