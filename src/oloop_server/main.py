"""The application. Behind a reverse proxy that ends TLS; this speaks plain HTTP."""
from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from oloop_server import __version__, auth, documents, errors, keys, projects
from oloop_server.config import Settings, get_settings
from oloop_server.db import make_engine
from oloop_server.netutil import parse_networks


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    parse_networks(settings.trusted_proxies)  # a wrong entry stops the start

    engine = make_engine(settings.database_url)
    app = FastAPI(
        title="oloop-server",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)

    errors.install(app)
    app.include_router(auth.router)
    app.include_router(keys.router)
    app.include_router(projects.router)
    app.include_router(documents.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Alive. No data and no authentication (spec/SERVER.md, "API v1")."""
        return {"status": "ok"}

    return app
