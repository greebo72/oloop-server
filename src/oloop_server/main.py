"""The application. Behind a reverse proxy that ends TLS; this speaks plain HTTP."""
import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import sessionmaker

from oloop_server import (
    __version__,
    agents,
    agentself,
    auth,
    blobs,
    documents,
    errors,
    keys,
    projects,
)
from oloop_server.blobstore import collect_garbage
from oloop_server.config import Settings, get_settings
from oloop_server.db import make_engine
from oloop_server.housekeeping import purge_expired_tokens
from oloop_server.netutil import parse_networks

log = logging.getLogger("oloop_server")


async def _run_regularly(every_seconds: float, what: str, job) -> None:
    while True:
        await asyncio.sleep(every_seconds)
        try:
            await run_in_threadpool(job)
        except Exception:
            # The next round tries again; the reason is in the log.
            log.exception("the clean-up of %s failed", what)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = app.state.settings
    jobs = []
    if settings.blob_gc_interval_minutes > 0:
        jobs.append((settings.blob_gc_interval_minutes, "the blobs", lambda: collect_garbage(
            app.state.session_factory, settings.blob_dir)))
    if settings.token_purge_interval_minutes > 0:
        jobs.append((settings.token_purge_interval_minutes, "the tokens",
                     lambda: purge_expired_tokens(app.state.session_factory)))
    tasks = [asyncio.create_task(_run_regularly(minutes * 60, what, job))
             for minutes, what, job in jobs]
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


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
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = sessionmaker(engine, expire_on_commit=False)

    errors.install(app)
    app.include_router(auth.router)
    app.include_router(keys.router)
    app.include_router(agents.router)
    app.include_router(agentself.router)
    app.include_router(projects.router)
    app.include_router(documents.router)
    app.include_router(blobs.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        """Alive. No data and no authentication (spec/SERVER.md, "API v1")."""
        return {"status": "ok"}

    return app
