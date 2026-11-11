"""FastAPI application and health endpoint."""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from functools import partial

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from invoicebot.store.db import create_pool, ping

logger = logging.getLogger(__name__)

DbPing = Callable[[], Awaitable[bool]]
Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


async def _no_db_ping() -> bool:
    """Database ping used when DATABASE_URL is not configured."""
    return False


def _log_config_reload_request() -> None:
    """Acknowledge SIGHUP; the actual config reload arrives in a later phase."""
    logger.info("config reload requested (SIGHUP)")


@asynccontextmanager
async def _database_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the pool from DATABASE_URL at startup and close it on shutdown."""
    dsn = os.environ.get("DATABASE_URL")
    pool = create_pool(dsn) if dsn else None
    if pool is not None:
        # Do not block startup when the database is down; /health reports it.
        await pool.open(wait=False)
        app.state.db_ping = partial(ping, pool)

    loop = asyncio.get_running_loop()
    sighup_installed = False
    try:
        loop.add_signal_handler(signal.SIGHUP, _log_config_reload_request)
        sighup_installed = True
    except (NotImplementedError, RuntimeError, ValueError):
        # Platforms or threads without signal support still start normally.
        pass

    try:
        yield
    finally:
        if sighup_installed:
            loop.remove_signal_handler(signal.SIGHUP)
        if pool is not None:
            await pool.close()


def create_app(db_ping: DbPing, *, lifespan: Lifespan | None = None) -> FastAPI:
    """Build the application with the given database ping callable."""
    app = FastAPI(lifespan=lifespan)
    app.state.db_ping = db_ping

    @app.get("/health")
    async def health() -> JSONResponse:
        """Report service health, degrading when the database ping fails."""
        try:
            db_ok = await app.state.db_ping()
        except Exception:  # any ping failure means degraded
            db_ok = False
        if db_ok:
            return JSONResponse({"status": "ok", "db": "ok"}, status_code=200)
        return JSONResponse({"status": "degraded", "db": "down"}, status_code=503)

    return app


app = create_app(_no_db_ping, lifespan=_database_lifespan)
