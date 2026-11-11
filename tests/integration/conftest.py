"""Integration-test fixtures: a migrated test database and an open pool."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest_asyncio
from alembic import command
from alembic.config import Config
from psycopg import AsyncConnection, conninfo, sql
from psycopg_pool import AsyncConnectionPool

from invoicebot.store.db import create_pool

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TEST_DATABASE_URL = "postgresql://invoicebot:invoicebot@localhost:5432/invoicebot_test"
MAINTENANCE_DB = "postgres"


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def _maintenance_dsn(url: str) -> tuple[str, str]:
    """Split a test URL into the maintenance-server DSN and the database name."""
    params = conninfo.conninfo_to_dict(url)
    dbname = params.get("dbname")
    if not isinstance(dbname, str) or not dbname:
        raise RuntimeError("TEST_DATABASE_URL must name a database")
    if not dbname.endswith("_test"):
        # The fixture drops this database, so refuse anything that is not a test one.
        raise RuntimeError(f"refusing to reset database {dbname!r}: its name must end with '_test'")
    return conninfo.make_conninfo(**{**params, "dbname": MAINTENANCE_DB}), dbname


async def _reset_database(url: str) -> None:
    """Drop and recreate the test database, failing if Postgres is unreachable."""
    maintenance_dsn, dbname = _maintenance_dsn(url)
    conn = await AsyncConnection.connect(maintenance_dsn, autocommit=True)
    async with conn:
        await conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(dbname)))
        await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))


def _upgrade(url: str) -> None:
    """Run `alembic upgrade head` against the test database."""
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            del os.environ["DATABASE_URL"]
        else:
            os.environ["DATABASE_URL"] = previous


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool() -> AsyncIterator[AsyncConnectionPool[AsyncConnection[Any]]]:
    """Reset, migrate and yield an open pool for the test database."""
    url = _test_database_url()
    await _reset_database(url)
    await asyncio.to_thread(_upgrade, url)
    pool = create_pool(url)
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()
