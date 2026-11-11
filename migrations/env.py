"""Alembic environment: online migrations against DATABASE_URL only."""

import os

from alembic import context
from sqlalchemy import create_engine, pool


def _database_url() -> str:
    """Read DATABASE_URL and adapt its scheme for the psycopg 3 driver."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def run_migrations_online() -> None:
    """Run migrations with a live connection; offline mode is not supported."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
