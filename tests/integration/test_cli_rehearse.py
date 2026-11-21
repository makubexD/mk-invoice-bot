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

from invoicebot.channels.base import Button, OutboundMessage
from invoicebot.channels.console import console_transport, inbox_dir
from invoicebot.cli import main
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ConsoleChannel
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.queue.outbox import OutboxSender, add, rows_after
from invoicebot.rehearse import RehearseError, RehearseSend, rehearse_replies, rehearse_send
from invoicebot.settings import Settings
from invoicebot.store.invoices import create_invoice, set_state

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


async def _last_console_id(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> int:
    """Return the newest console outbox id, or 0 when there is none."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT coalesce(max(id), 0) FROM outbox WHERE channel = 'console'")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _two_open_invoices(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str
) -> tuple[int, int, int]:
    """Create two ``MANUAL_REQUIRED`` console invoices with a delivered BR-F07 message each.

    Returns the first invoice's id, the second's id and the first invoice's outbox id,
    whose channel message id is the ``--reply-to`` value a quoted upload passes
    (D102, D165).
    """
    async with pool.connection() as conn:
        first = await create_invoice(conn, profile_id, "console")
        await set_state(conn, first, "MANUAL_REQUIRED", rule_id="BR-F07")
        quoted = await queue_message(
            conn,
            Addressee("console", profile_id),
            "Synthetic bot message.",
            invoice_id=first,
            rule_id="BR-F07",
        )
        second = await create_invoice(conn, profile_id, "console")
        await set_state(conn, second, "MANUAL_REQUIRED", rule_id="BR-F07")
        await queue_message(
            conn,
            Addressee("console", profile_id),
            "Synthetic bot message.",
            invoice_id=second,
            rule_id="BR-F07",
        )
    await OutboxSender(pool, {"console": console_transport()}).send_pending()
    return first, second, quoted


async def _invoice_jobs(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    """Return invoice_id's job steps, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
        return [str(row[0]) for row in await cur.fetchall()]


async def _messages_invoice_ids(
    pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str
) -> list[int | None]:
    """Return the invoice ids the profile's console messages are linked to, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT invoice_id FROM messages WHERE profile_id = %s AND channel = 'console'"
            " ORDER BY received_at",
            (profile_id,),
        )
        return [None if row[0] is None else int(row[0]) for row in await cur.fetchall()]


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
async def test_queued_photo_keeps_its_inbox_file(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: a photo that queues keeps its inbox file for the download job."""
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

    assert result.status == "queued"
    assert result.invoice_id is not None
    [inbox_file] = inbox_dir(tmp_path).glob("*.png")
    assert inbox_file.read_bytes() == data


@pytest.mark.asyncio(loop_scope="session")
async def test_lone_xml_leaves_no_inbox_file(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: a lone XML is refused (BR-E07) and its inbox file is removed."""
    profile_id = _profile_id()
    cfg = _config(profile_id)

    result = await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(name="factura.xml"),
        io.BytesIO(b"<factura/>"),
    )

    invoices, jobs, outbox = await _rows(db_pool, profile_id)
    assert invoices == []
    assert jobs == []
    assert outbox == [("console", "BR-E07")]
    assert result.status == "stored"
    assert result.invoice_id is None
    assert list(inbox_dir(tmp_path).iterdir()) == []


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


@pytest.mark.asyncio(loop_scope="session")
async def test_replies_returns_the_acknowledgement(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D165: after a rehearse send photo, rehearse_replies returns the BR-S09 reply."""
    profile_id = _profile_id()
    cfg = _config(profile_id)
    before = await _last_console_id(db_pool)

    await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(name="photo.png"),
        io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"photo"),
    )

    replies = await rehearse_replies(test_database_url, before)
    assert [reply.rule_id for reply in replies] == ["BR-S09"]
    assert replies[0].status == "pending"
    assert replies[0].text
    assert replies[0].buttons == []


@pytest.mark.asyncio(loop_scope="session")
async def test_rows_after_the_last_id_returns_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D165: rows_after with the newest console id returns no rows."""
    last_id = await _last_console_id(db_pool)

    async with db_pool.connection() as conn:
        assert await rows_after(conn, "console", last_id) == []


@pytest.mark.asyncio(loop_scope="session")
async def test_console_row_buttons_are_returned(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
) -> None:
    """D165: a console row with buttons comes back with its buttons."""
    profile_id = _profile_id()
    message = OutboundMessage(
        channel="console",
        recipient=profile_id,
        profile_id=profile_id,
        rule_id="BR-S01",
        text="¿Confirmas?",
        buttons=[Button(id="yes", title="Sí"), Button(id="no", title="No")],
    )
    async with db_pool.connection() as conn:
        outbox_id = await add(conn, "console", profile_id, message.model_dump(mode="json"))

    replies = await rehearse_replies(test_database_url, outbox_id - 1)
    [reply] = [other for other in replies if other.id == outbox_id]
    assert reply.rule_id == "BR-S01"
    assert reply.text == "¿Confirmas?"
    assert reply.buttons == [Button(id="yes", title="Sí"), Button(id="no", title="No")]


@pytest.mark.asyncio(loop_scope="session")
async def test_other_channels_are_never_returned(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
) -> None:
    """D165: rehearse replies never returns a row of another channel."""
    before = await _last_console_id(db_pool)
    async with db_pool.connection() as conn:
        whatsapp_id = await add(conn, "whatsapp", _profile_id(), {"text": "hola"})

    replies = await rehearse_replies(test_database_url, before)
    assert all(reply.id != whatsapp_id for reply in replies)
    assert replies == []


def test_replies_without_a_console_channel_exits_two(
    test_database_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D165: without a console channel rehearse replies exits 2 with the D165 message."""
    code = main(["rehearse", "replies"], settings=_settings(tmp_path, test_database_url))

    assert code == 2
    assert "console channel not configured (D165)" in capsys.readouterr().err


@pytest.mark.asyncio(loop_scope="session")
async def test_quoted_upload_links_to_the_quoted_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D102, D165: a quoted upload links to the quoted invoice instead of asking BR-D14."""
    profile_id = _profile_id()
    cfg = _config(profile_id)
    first, _second, quoted = await _two_open_invoices(db_pool, profile_id)

    result = await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(name="doc.pdf", reply_to=quoted),
        io.BytesIO(b"%PDF-1.4 synthetic"),
    )

    assert result.invoice_id == first
    assert await _messages_invoice_ids(db_pool, profile_id) == [first]
    assert await _invoice_jobs(db_pool, first) == ["attach_upload"]
    _invoices, _jobs, outbox = await _rows(db_pool, profile_id)
    assert [rule_id for _channel, rule_id in outbox] == ["BR-F07", "BR-F07"]
    assert len(list(inbox_dir(tmp_path).glob("*.pdf"))) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_unquoted_upload_asks_which_invoice(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    test_database_url: str,
    tmp_path: Path,
) -> None:
    """D102, D165: an unquoted upload with two open invoices asks which one (BR-D14)."""
    profile_id = _profile_id()
    cfg = _config(profile_id)
    first, second, _quoted = await _two_open_invoices(db_pool, profile_id)

    result = await rehearse_send(
        tmp_path,
        test_database_url,
        cfg,
        intake_settings(cfg),
        RehearseSend(name="doc.pdf"),
        io.BytesIO(b"%PDF-1.4 synthetic"),
    )

    assert result.invoice_id is None
    _invoices, _jobs, outbox = await _rows(db_pool, profile_id)
    assert [rule_id for _channel, rule_id in outbox] == ["BR-F07", "BR-F07", "BR-D14"]
    assert await _invoice_jobs(db_pool, first) == []
    assert await _invoice_jobs(db_pool, second) == []
    assert list(inbox_dir(tmp_path).iterdir()) == []
