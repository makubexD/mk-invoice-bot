"""Integration tests for ``invoicebot auth-microsoft`` (R-7.9, BR-D01)."""

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.auth.msal_cache import AuthRequired
from invoicebot.cli import main
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MailerSink
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.settings import Settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
ACCOUNT = "personal"
BASE_SCOPES = ("offline_access", "Files.ReadWrite")


class _StubProvider:
    """A TokenProvider stand-in that records the scopes and can fail the sign-in (R-7.9)."""

    def __init__(self, error: AuthRequired | None = None) -> None:
        self.error = error
        self.account = ""
        self.scopes: tuple[str, ...] = ()

    def login_device_code(self, show: Callable[[str], None]) -> None:
        """Record the request and raise the configured failure, if any."""
        if self.error is not None:
            raise self.error


def _factory(provider: _StubProvider) -> Callable[..., _StubProvider]:
    """Return the factory ``main`` calls, handing it the stub provider."""

    def build(settings: Settings, cfg: Config, account: str, scopes: Sequence[str]) -> _StubProvider:
        provider.account = account
        provider.scopes = tuple(scopes)
        return provider

    return build


def _settings(pool: AsyncConnectionPool[AsyncConnection[Any]], data_dir: Path, config: Path) -> Settings:
    """Build settings against the test database and the synthetic example config."""
    return Settings(config_path=config, database_url=pool.conninfo, data_dir=data_dir)


async def _run_cli(settings: Settings, provider: _StubProvider) -> int:
    """Run the command off the event loop, as the console entry point does."""
    return await asyncio.to_thread(
        main,
        ["auth-microsoft", "--account", ACCOUNT],
        settings=settings,
        token_provider_factory=_factory(provider),
    )


async def _seed_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], state: str) -> int:
    """Insert one invoice in ``state`` and return its id."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO invoices (profile_id, origin_channel, state)
            VALUES (%s, 'whatsapp', %s)
            RETURNING id
            """,
            (f"profile-{uuid4().hex}", state),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> str:
    """Return the invoice's state."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _job_steps(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's jobs."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s", (invoice_id,))
        return [str(row[0]) for row in await cur.fetchall()]


async def _count_pending(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> int:
    """Return how many invoices are waiting for a sign-in."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM invoices WHERE state = 'PENDING_AUTH'")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _config_without_graph(tmp_path: Path) -> Path:
    """Return a copy of the example config whose mailer sends only as a dry run."""
    cfg = load_config(EXAMPLE_CONFIG)
    flows = {
        name: flow.model_copy(
            update={
                "sinks": [
                    sink.model_copy(update={"modes": ["dry_run"]}) if isinstance(sink, MailerSink) else sink
                    for sink in flow.sinks
                ]
            }
        )
        for name, flow in cfg.flows.items()
    }
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(cfg.model_copy(update={"flows": flows}).model_dump(mode="json")),
        encoding="utf-8",
    )
    return path


async def test_auth_microsoft_resumes_every_pending_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-7.9: a successful sign-in moves every PENDING_AUTH invoice to DELIVERING with a deliver job."""
    pending = [await _seed_invoice(db_pool, "PENDING_AUTH") for _ in range(2)]
    done = await _seed_invoice(db_pool, "DONE")
    expected = await _count_pending(db_pool)

    code = await _run_cli(_settings(db_pool, tmp_path / "data", EXAMPLE_CONFIG), _StubProvider())

    assert code == 0
    for invoice_id in pending:
        assert await _state(db_pool, invoice_id) == "DELIVERING"
        assert await _job_steps(db_pool, invoice_id) == [DELIVER_STEP]
    assert await _state(db_pool, done) == "DONE"
    assert await _job_steps(db_pool, done) == []
    assert f"resumed {expected} invoice(s)" in capsys.readouterr().out


async def test_auth_microsoft_failed_login_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BR-D01: a failed sign-in exits 1 and leaves the invoice PENDING_AUTH without a deliver job."""
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    code = await _run_cli(
        _settings(db_pool, tmp_path / "data", EXAMPLE_CONFIG),
        _StubProvider(AuthRequired(ACCOUNT)),
    )

    assert code == 1
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    assert f"Microsoft sign-in needed for {ACCOUNT}" in capsys.readouterr().err


@pytest.mark.parametrize(("graph", "expected"), [(True, True), (False, False)])
async def test_auth_microsoft_mail_scope_follows_the_graph_mailer(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    graph: bool,
    expected: bool,
) -> None:
    """R-7.9: Mail.Send is requested only when a flow's mailer sends through Graph."""
    config = EXAMPLE_CONFIG if graph else _config_without_graph(tmp_path)
    provider = _StubProvider()

    code = await _run_cli(_settings(db_pool, tmp_path / "data", config), provider)

    assert code == 0
    assert provider.account == ACCOUNT
    assert provider.scopes[: len(BASE_SCOPES)] == BASE_SCOPES
    assert ("Mail.Send" in provider.scopes) is expected
