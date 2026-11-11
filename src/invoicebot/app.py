"""FastAPI application and health endpoint."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse


async def _placeholder_ping() -> bool:
    """Database ping placeholder; wired to the real pool in step 6."""
    return False


def create_app(db_ping: Callable[[], Awaitable[bool]]) -> FastAPI:
    """Build the application with the given database ping callable."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> JSONResponse:
        """Report service health, degrading when the database ping fails."""
        try:
            db_ok = await db_ping()
        except Exception:  # any ping failure means degraded
            db_ok = False
        if db_ok:
            return JSONResponse({"status": "ok", "db": "ok"}, status_code=200)
        return JSONResponse({"status": "degraded", "db": "down"}, status_code=503)

    return app


app = create_app(_placeholder_ping)
