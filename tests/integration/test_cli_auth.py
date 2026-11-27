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
from invoicebot.auth.providers import SMTP_SEND_SCOPE, scopes_for
from invoicebot.cli import main
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MailerSink
from invoicebot.pipeline.deliver import DELIVER_STEP
from invoicebot.pipeline.pending_auth import MICROSOFT
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
        self.added: list[tuple[str, ...]] = []
        self.add_error: AuthRequired | None = None

    def login_device_code(self, show: Callable[[str], None]) -> None:
        """Record the request and raise the configured failure, if any."""
        if self.error is not None:
            raise self.error

    def add_scopes_device_code(self, show: Callable[[str], None], scopes: Sequence[str]) -> None:
        """Record the extra scopes and raise the second flow's failure, if any (D205)."""
        self.added.append(tuple(scopes))
        if self.add_error is not None:
            raise self.add_error


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


async def _run_cli(settings: Settings, provider: _StubProvider, account: str = ACCOUNT) -> int:
    """Run the command off the event loop, as the console entry point does."""
    return await asyncio.to_thread(
        main,
        ["auth-microsoft", "--account", account],
        settings=settings,
        token_provider_factory=_factory(provider),
    )


async def _seed_invoice(pool: AsyncConnectionPool[AsyncConnection[Any]], state: str) -> int:
    """Insert one invoice in ``state``, parked for this file's account, and return its id."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            INSERT INTO invoices (profile_id, origin_channel, state, error)
            VALUES (%s, 'whatsapp', %s, %s)
            RETURNING id
            """,
            (f"profile-{uuid4().hex}", state, f"{MICROSOFT}:{ACCOUNT}"),
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
    """Return how many invoices are waiting for this file's account to sign in."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM invoices WHERE state = 'PENDING_AUTH' AND error = %s",
            (f"{MICROSOFT}:{ACCOUNT}",),
        )
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _config_with_smtp(tmp_path: Path) -> Path:
    """Return a copy of the example config whose mailer also lists ``smtp`` (D205)."""
    cfg = load_config(EXAMPLE_CONFIG)
    flows = {
        name: flow.model_copy(
            update={
                "sinks": [
                    sink.model_copy(update={"modes": [*sink.modes, "smtp"]})
                    if isinstance(sink, MailerSink)
                    else sink
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


async def test_scopes_for_personal_cover_files_and_mail_send() -> None:
    """R-6.5, R-7.9, D98: the example's delivery account gets files and mail send, no mail read."""
    cfg = load_config(EXAMPLE_CONFIG)

    assert scopes_for(cfg, ACCOUNT) == ("offline_access", "Files.ReadWrite", "Mail.Send")


async def test_scopes_for_smtp_account_gets_mail_readwrite(tmp_path: Path) -> None:
    """D207: an account with an smtp mailer also signs in with Mail.ReadWrite."""
    cfg = load_config(_config_with_smtp(tmp_path))

    assert "Mail.ReadWrite" in scopes_for(cfg, ACCOUNT)


async def test_scopes_for_graph_or_onedrive_only_account_has_no_mail_readwrite() -> None:
    """D207: a graph-only mailer account and an inbox-only account do not get Mail.ReadWrite."""
    cfg = load_config(EXAMPLE_CONFIG)

    assert "Mail.ReadWrite" not in scopes_for(cfg, ACCOUNT)
    assert "Mail.ReadWrite" not in scopes_for(cfg, "bot")


async def test_scopes_for_smtp_account_that_reads_its_inbox_has_each_scope_once(tmp_path: Path) -> None:
    """D207: an smtp account that also reads its inbox gets Mail.Read and Mail.ReadWrite once each."""
    cfg = load_config(_config_with_smtp(tmp_path))
    flows = {
        name: flow.model_copy(
            update={"email_inbox": flow.email_inbox.model_copy(update={"account": ACCOUNT})}
        )
        if flow.email_inbox is not None
        else flow
        for name, flow in cfg.flows.items()
    }
    reading = cfg.model_copy(update={"flows": flows})

    scopes = scopes_for(reading, ACCOUNT)

    assert scopes.count("Mail.Read") == 1
    assert scopes.count("Mail.ReadWrite") == 1


async def test_scopes_for_bot_reads_the_inbox_only() -> None:
    """R-6.5, R-7.9, D98: the inbox-only account gets Mail.Read and no delivery scope."""
    cfg = load_config(EXAMPLE_CONFIG)

    assert scopes_for(cfg, "bot") == ("offline_access", "Mail.Read")


async def test_scopes_for_bot_sends_mail_when_a_flow_lists_inbox_rule() -> None:
    """R-7.7, D108: a flow listing inbox_rule gives the bot account Mail.Send."""
    cfg = load_config(EXAMPLE_CONFIG)
    name, flow = next(
        (name, flow)
        for name, flow in cfg.flows.items()
        if any(isinstance(sink, MailerSink) for sink in flow.sinks)
    )
    sinks = [
        sink.model_copy(update={"modes": [*sink.modes, "inbox_rule"]})
        if isinstance(sink, MailerSink)
        else sink
        for sink in flow.sinks
    ]
    listed = cfg.model_copy(update={"flows": {**cfg.flows, name: flow.model_copy(update={"sinks": sinks})}})

    assert "Mail.Send" in scopes_for(listed, "bot")


async def test_auth_microsoft_unknown_account_exits_two_and_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5, R-7.9, D98: an account no flow uses exits 2 and leaves PENDING_AUTH alone."""
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")

    with pytest.raises(SystemExit) as exit_info:
        await _run_cli(_settings(db_pool, tmp_path / "data", EXAMPLE_CONFIG), _StubProvider(), "nobody")

    assert exit_info.value.code == 2
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    error = capsys.readouterr().err
    assert "nobody" in error
    assert ACCOUNT in error
    assert "bot" in error


async def test_auth_microsoft_bot_signs_in_without_resuming(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """R-6.5, R-7.9, D98: signing the inbox account in resumes no PENDING_AUTH invoice."""
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")
    provider = _StubProvider()

    code = await _run_cli(_settings(db_pool, tmp_path / "data", EXAMPLE_CONFIG), provider, "bot")

    assert code == 0
    assert provider.account == "bot"
    assert provider.scopes == ("offline_access", "Mail.Read")
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    assert "resumed 0 invoice(s)" in capsys.readouterr().out


async def test_auth_microsoft_smtp_account_runs_a_second_flow_then_resumes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D205: an account with an smtp mailer runs a second device flow for SMTP.Send, then resumes."""
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")
    provider = _StubProvider()

    code = await _run_cli(_settings(db_pool, tmp_path / "data", _config_with_smtp(tmp_path)), provider)

    assert code == 0
    assert provider.added == [(SMTP_SEND_SCOPE,)]
    assert await _state(db_pool, invoice_id) == "DELIVERING"
    assert "a second code adds sending through SMTP" in capsys.readouterr().out


async def test_auth_microsoft_failed_second_flow_resumes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """D205: a failed SMTP consent exits 1 and leaves the invoice PENDING_AUTH."""
    invoice_id = await _seed_invoice(db_pool, "PENDING_AUTH")
    provider = _StubProvider()
    provider.add_error = AuthRequired(ACCOUNT)

    code = await _run_cli(_settings(db_pool, tmp_path / "data", _config_with_smtp(tmp_path)), provider)

    assert code == 1
    assert await _state(db_pool, invoice_id) == "PENDING_AUTH"
    assert await _job_steps(db_pool, invoice_id) == []
    assert f"Microsoft sign-in needed for {ACCOUNT}" in capsys.readouterr().err


async def test_auth_microsoft_without_smtp_runs_one_flow(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """D205: an account with no smtp mailer runs one device flow, as before."""
    provider = _StubProvider()

    code = await _run_cli(_settings(db_pool, tmp_path / "data", EXAMPLE_CONFIG), provider)

    assert code == 0
    assert provider.added == []
