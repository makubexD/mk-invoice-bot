"""Unit tests for the pg_dump child's environment (R-9.14)."""

import pytest

from invoicebot.ops.backup import _pg_environment


def test_pg_environment_maps_sslmode_query() -> None:
    """R-9.14: a ``?sslmode=require`` query sets PGSSLMODE=require."""
    env = _pg_environment("postgresql://invoicebot@db:5432/invoicebot?sslmode=require")

    assert env["PGSSLMODE"] == "require"


def test_pg_environment_rejects_unknown_query_key() -> None:
    """R-9.14: an unknown query key raises ValueError naming the key and not its value."""
    url = "postgresql://invoicebot@db/invoicebot?options=do-not-leak-me-42"

    with pytest.raises(ValueError, match="options") as raised:
        _pg_environment(url)

    assert "do-not-leak-me-42" not in str(raised.value)


def test_pg_environment_without_query_sets_no_ssl_variable() -> None:
    """R-9.14: a URL with no query sets no PGSSL* variable."""
    env = _pg_environment("postgresql://invoicebot:secret@db:5432/invoicebot")

    assert not any(key.startswith("PGSSL") for key in env)


def test_pg_environment_keeps_host_case() -> None:
    """R-9.14: a mixed-case host keeps the case written in the URL."""
    env = _pg_environment("postgresql://invoicebot@Db.Example.COM:5432/invoicebot")

    assert env["PGHOST"] == "Db.Example.COM"
