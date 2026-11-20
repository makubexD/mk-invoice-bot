"""Integration tests for ``invoicebot rehearse send`` (D165).

Each test feeds one message through the real intake against real Postgres, using the
example config with a dev-only console channel. The inbox directory is a per-test
``tmp_path`` and stdin is a ``BytesIO``, so no test touches the real terminal.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.app import intake_settings
from invoicebot.channels.console import inbox_dir
from invoicebot.cli import main
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ConsoleChannel
from invoicebot.rehearse import RehearseError, RehearseSend, rehearse_send
from invoicebot.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config" / "example.yaml"


def _profile_id() -> str:
    """Return a profile id no other test uses."""
    return f"console-{uuid4().hex}"


def _config(profile_id: str, *, image_cap: int | None = None) -> Config:
    """Return the example config with a console channel speaking as ``profile_id``."""
    cfg = load_config(EXAMPLE)
    profile = cfg.profiles["yo"]
    update: dict[str, Any] = {
        "owner": cfg.owner.model_copy(update={"profile": profile_id}),
        "profiles": {profile_id: profile},
        "channels": [*cfg.channels, ConsoleChannel(kind="console", profile=profile_id)],
    }
    if image_cap is not None:
        max_bytes = cfg.limits.media.max_bytes.model_copy(update={"image": image_cap})
        media = cfg.limits.media.model_copy(update={"max_bytes": max_bytes})
        update["limits"] = cfg.limits.model_copy(update={"media": media})
    return cfg.model_copy(update=update)


def _settings(tmp_path: Path, database_url: str) -> Settings:
    """Return process settings pointing the console inbox at ``tmp_path``."""
    return Settings(database_url=database_url, config_path=EXAMPLE, data_dir=tmp_path)


async def _rows(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str
) -> tuple[list[Any], list[str], list[Any]]:
    """Return the profile's invoice channels, job steps and outbox ``(channel, rule_id)`` rows."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT origin_channel FROM invoices WHERE profile_id = %s ORDER BY id",
            (profile_id,),
        )
        invoices = [row[0] for row in await cur.fetchall()]
        cur = await conn.execute(
            """
            SELECT j.step
            FROM jobs AS j
            JOIN invoices AS i ON i.id = j.invoice_id
            WHERE i.profile_id = %s
            ORDER BY j.id
            """,
            (profile_id,),
        )
        jobs = [str(row[0]) for row in await cur.fetchall()]
        cur = await conn.execute(
            """
            SELECT channel, payload_json->>'rule_id'
            FROM outbox
            WHERE "to" = %s
            ORDER BY id
            """,
            (profile_id,),
        )
        outbox = [(row[0], row[1]) for row in await cur.fetchall()]
    return invoices, jobs, outbox


@pytest.mark.asyncio(loop_scope="session")
async def test_photo_creates_invoice_job_and_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: a photo from stdin makes one console invoice, its download job and BR-S09."""
    profile_id = _profile_id()
    cfg = _config(profile_id)
    data = b"\x89PNG\r\n\x1a\n" + b"photo"

    result = await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(name="photo.png"),
        io.BytesIO(data),
    )

    invoices, jobs, outbox = await _rows(db_pool, profile_id)
    assert invoices == ["console"]
    assert jobs == ["download_media"]
    assert outbox == [("console", "BR-S09")]
    inbox_files = list(inbox_dir(tmp_path).glob("*.png"))
    assert len(inbox_files) == 1
    assert inbox_files[0].read_bytes() == data
    assert result.status == "queued"
    assert result.invoice_id is not None


@pytest.mark.asyncio(loop_scope="session")
async def test_text_help_stores_a_reply_and_no_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: ``--text /ayuda`` stores the help reply and creates no invoice."""
    profile_id = _profile_id()
    cfg = _config(profile_id)

    await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(text="/ayuda"),
        io.BytesIO(),
    )

    invoices, jobs, outbox = await _rows(db_pool, profile_id)
    assert invoices == []
    assert jobs == []
    assert len(outbox) == 1
    assert outbox[0][0] == "console"
    assert outbox[0][1] is None


@pytest.mark.asyncio(loop_scope="session")
async def test_stdin_over_the_cap_is_refused_and_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: a photo over its cap is refused before any inbox file or row is written."""
    profile_id = _profile_id()
    cfg = _config(profile_id, image_cap=10)

    with pytest.raises(RehearseError, match="over the photo cap"):
        await rehearse_send(
            tmp_path,
            test_database_url,
            cfg,
            intake_settings(cfg),
            RehearseSend(name="photo.png"),
            io.BytesIO(b"x" * 11),
        )

    assert not inbox_dir(tmp_path).exists()
    invoices, jobs, outbox = await _rows(db_pool, profile_id)
    assert invoices == []
    assert jobs == []
    assert outbox == []


@pytest.mark.asyncio(loop_scope="session")
async def test_unknown_extension_is_refused(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: a file name whose extension is not supported is refused."""
    profile_id = _profile_id()
    cfg = _config(profile_id)

    with pytest.raises(RehearseError, match="unsupported file type"):
        await rehearse_send(
            tmp_path,
            test_database_url,
            cfg,
            intake_settings(cfg),
            RehearseSend(name="x.exe"),
            io.BytesIO(b"not used"),
        )

    invoices, jobs, outbox = await _rows(db_pool, profile_id)
    assert invoices == []
    assert jobs == []
    assert outbox == []


def test_no_console_channel_exits_two(
    test_database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D165: without a console channel the command exits 2 with the D165 message."""
    code = main(
        ["rehearse", "send", "--text", "/ayuda"],
        settings=_settings(tmp_path, test_database_url),
    )

    assert code == 2
    assert "console channel not configured (D165)" in capsys.readouterr().err
