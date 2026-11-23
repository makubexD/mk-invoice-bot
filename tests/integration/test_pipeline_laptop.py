"""Integration tests for the laptop receipt poller (R-7.8, R-7.14, BR-D10, BR-D11, D108).

The drive is an in-memory fake; Postgres is real. Each test builds a prepared batch
with one stored mail job and writes the receipt the laptop agent would write.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.buttons import button_id
from invoicebot.config.models import Mail
from invoicebot.extract.fields import Field, Reading
from invoicebot.pipeline.actions import ARRIVED_ACTION, SELF_SEND_ACTION, VIA_LAPTOP_ACTION
from invoicebot.pipeline.laptop import RECEIPT_MAX_BYTES, check_overdue, poll_receipts
from invoicebot.rules.messages_es import (
    BUTTON_ARRIVED,
    BUTTON_SELF_SEND,
    BUTTON_VIA_LAPTOP,
    MESSAGES,
)
from invoicebot.sinks.onedrive import FileTooLarge
from invoicebot.store.flow_b import get_batch_state, merge_flow_data, open_batch, set_batch_state
from invoicebot.store.invoices import (
    FileRecord,
    add_file,
    create_invoice,
    get_file_path,
    save_reading,
    set_flow,
    set_state,
)
from invoicebot.store.mail_jobs import add_mail_job, set_receipt_at

pytestmark = pytest.mark.asyncio(loop_scope="session")

FLOW = "work_microsoft"
LAPTOP_FLOWS = frozenset({FLOW})
ROOT = "/Reembolsos"
RECEIPTS = f"{ROOT}/_outbox/receipts"
FOLDER = "2026-11-Support_1_LaLena"
SERIE = "F001"
NUMERO = "15160"
FECHA = date(2026, 11, 5)
SENT_AT = "2026-11-16T10:00:00+00:00"
ERROR_TEXT = "Outlook no está disponible"
KEY = b"test-receipt-key"


class FakeDrive:
    """An in-memory Drive: a dict of absolute paths to bytes, listed one folder at a time."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.downloads: list[str] = []
        self.deleted: list[str] = []

    async def list_names(self, path: str) -> list[str]:
        """Return the names directly under ``path``."""
        prefix = path.rstrip("/") + "/"
        return [
            name[len(prefix) :]
            for name in self.files
            if name.startswith(prefix) and "/" not in name[len(prefix) :]
        ]

    async def download(self, path: str, max_bytes: int) -> bytes:
        """Return the file's bytes, refusing a body over ``max_bytes``."""
        self.downloads.append(path)
        data = self.files[path]
        if len(data) > max_bytes:
            raise FileTooLarge(max_bytes)
        return data

    async def delete(self, path: str) -> None:
        """Record the delete and drop the file; deleting a missing file is accepted."""
        self.deleted.append(path)
        self.files.pop(path, None)


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a synthetic reading with the serie and numero BR-D11 names."""
    return Reading(
        fields={
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "fecha_emision": _field(FECHA.isoformat()),
        },
        document_url=None,
    )


def _signed_receipt(fields: dict[str, str], key: bytes = KEY) -> bytes:
    """Return a receipt file carrying the HMAC the laptop agent writes."""
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    signature = hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()
    return json.dumps({**fields, "hmac": signature}).encode()


def _sent_receipt(job_id: str) -> bytes:
    """Return the signed receipt the laptop writes after it sends the job."""
    return _signed_receipt({"type": "receipt", "job_id": job_id, "sent_at": SENT_AT})


def _error_receipt(job_id: str, error: str = ERROR_TEXT) -> bytes:
    """Return the signed receipt the laptop writes when Outlook fails."""
    return _signed_receipt({"type": "receipt", "job_id": job_id, "error": error})


def _forged_receipt(job_id: str) -> bytes:
    """Return a sent receipt signed with a key other than the bot's."""
    return _signed_receipt({"type": "receipt", "job_id": job_id, "sent_at": SENT_AT}, key=b"another-key")


def _path(name: str) -> str:
    """Return the drive path of a receipt file."""
    return f"{RECEIPTS}/{name}"


def _drive(name: str, data: bytes) -> FakeDrive:
    """Return a drive holding one receipt file."""
    return FakeDrive({_path(name): data})


def _job_file(job_id: str) -> str:
    """Return the drive path of the job file the bot hands to the laptop agent."""
    return f"{ROOT}/_outbox/{job_id}.json"


async def _age_job(pool: AsyncConnectionPool[AsyncConnection[Any]], job_id: str, hours: int) -> None:
    """Move the job's creation time back by ``hours`` on the database clock."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE mail_jobs SET created_at = now() - make_interval(hours => %s) WHERE job_id = %s",
            (hours, job_id),
        )


async def _prepared_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    job_id: str,
) -> int:
    """Create a BATCHED invoice whose batch is prepared and whose mail job is stored."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, "BATCHED", rule_id="BR-D07")
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, "folder_path": f"{ROOT}/{FOLDER}"})
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "prepared")
        await add_mail_job(conn, job_id, invoice_id)
    return invoice_id


async def _prepared_invoice_without_flow(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    job_id: str,
) -> int:
    """Create a BATCHED invoice with a prepared batch, a mail job and no flow yet."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_state(conn, invoice_id, "BATCHED", rule_id="BR-D07")
        await merge_flow_data(conn, invoice_id, {"folder": FOLDER, "folder_path": f"{ROOT}/{FOLDER}"})
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "prepared")
        await add_mail_job(conn, job_id, invoice_id)
    return invoice_id


async def _add_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, tmp_path: Path
) -> Path:
    """Store a synthetic photo for the invoice and return its local path."""
    photo = tmp_path / f"{uuid4().hex}.jpg"
    photo.write_bytes(b"\xff\xd8\xff synthetic photo\n")
    async with pool.connection() as conn, conn.transaction():
        await add_file(
            conn,
            invoice_id,
            FileRecord(
                kind="photo",
                local_path=str(photo),
                remote_ref=None,
                sha256="0" * 64,
                size=photo.stat().st_size,
            ),
        )
    return photo


async def _invoice_state(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


async def _jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the steps of the invoice's queued jobs, in insertion order."""
    cur = await conn.execute("SELECT step FROM jobs WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(step) for (step,) in await cur.fetchall()]


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None, Any]]:
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id', payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    rows = await cur.fetchall()
    return [(str(text), rule_id, buttons) for text, rule_id, buttons in rows]


async def _receipt_row(conn: AsyncConnection[Any], job_id: str) -> tuple[Any, Any]:
    cur = await conn.execute(
        "SELECT receipt_at, receipt_error FROM mail_jobs WHERE job_id = %s",
        (job_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    return row[0], row[1]


async def test_a_sent_receipt_completes_the_batch_with_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.8, R-7.14, BR-D10: a sent receipt closes the batch, files DONE and queues folder_status."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    photo = await _add_photo(db_pool, invoice_id, tmp_path)

    await poll_receipts(db_pool, _drive(f"{job_id}.json", _sent_receipt(job_id)), ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        receipt_at, receipt_error = await _receipt_row(conn, job_id)
        photo_path = await get_file_path(conn, invoice_id, "photo")
    assert invoice == ("DONE", "BR-D10")
    assert batch_state == "sent"
    assert jobs == ["folder_status"]
    assert outbox == [(MESSAGES["BR-D10"].format(folder=FOLDER), "BR-D10", [])]
    assert receipt_at is not None
    assert receipt_error is None
    assert not photo.exists()
    assert photo_path is None


async def test_the_same_receipt_seen_again_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: a receipt already applied neither re-notifies nor re-stamps the job."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    drive = _drive(f"{job_id}.json", _sent_receipt(job_id))

    await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)
    async with db_pool.connection() as conn:
        first_outbox = await _outbox(conn, profile_id)
        first_jobs = await _jobs(conn, invoice_id)
        first_receipt = await _receipt_row(conn, job_id)

    await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        jobs = await _jobs(conn, invoice_id)
        receipt = await _receipt_row(conn, job_id)
    assert invoice == ("DONE", "BR-D10")
    assert batch_state == "sent"
    assert outbox == first_outbox
    assert jobs == first_jobs
    assert receipt == first_receipt


async def test_an_error_receipt_sends_br_d11_and_leaves_the_batch_prepared(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8, BR-D11: an error receipt records the error, asks the sender, and changes no state."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)

    await poll_receipts(db_pool, _drive(f"{job_id}.json", _error_receipt(job_id)), ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        receipt_at, receipt_error = await _receipt_row(conn, job_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert jobs == []
    assert outbox == [
        (
            MESSAGES["BR-D11"].format(serie=SERIE, numero=NUMERO),
            "BR-D11",
            [
                {"id": button_id(invoice_id, VIA_LAPTOP_ACTION), "title": BUTTON_VIA_LAPTOP},
                {"id": button_id(invoice_id, SELF_SEND_ACTION), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, ARRIVED_ACTION), "title": BUTTON_ARRIVED},
            ],
        ),
    ]
    assert receipt_at is None
    assert receipt_error == ERROR_TEXT


async def test_an_error_receipt_for_a_flow_outside_laptop_flows_omits_the_laptop_button(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8, D109: a flow not listed in laptop_flows gets BR-D11 without [Por la laptop]."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)

    await poll_receipts(
        db_pool,
        _drive(f"{job_id}.json", _error_receipt(job_id)),
        ROOT,
        KEY,
        frozenset({"another_flow"}),
    )

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == [
        (
            MESSAGES["BR-D11"].format(serie=SERIE, numero=NUMERO),
            "BR-D11",
            [
                {"id": button_id(invoice_id, SELF_SEND_ACTION), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, ARRIVED_ACTION), "title": BUTTON_ARRIVED},
            ],
        ),
    ]


async def test_a_receipt_over_four_kib_is_skipped_and_logged(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-7.14: a receipt over 4 KiB is abandoned while downloading and logged by file name."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    oversized = b"{" + b" " * RECEIPT_MAX_BYTES + b"}"
    name = f"{job_id}.json"

    with caplog.at_level(logging.WARNING, logger="invoicebot.pipeline.laptop"):
        await poll_receipts(db_pool, _drive(name, oversized), ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        receipt = await _receipt_row(conn, job_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert jobs == []
    assert outbox == []
    assert receipt == (None, None)
    assert name in caplog.text


async def test_a_receipt_for_an_unknown_job_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a receipt naming an id no mail job has changes no row."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    unknown_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)

    await poll_receipts(
        db_pool, _drive(f"{unknown_id}.json", _sent_receipt(unknown_id)), ROOT, KEY, LAPTOP_FLOWS
    )

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        receipt = await _receipt_row(conn, job_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert jobs == []
    assert outbox == []
    assert receipt == (None, None)


async def test_a_failing_receipt_is_logged_and_the_next_receipt_is_applied(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-7.14: a receipt whose job's invoice has no flow is logged and left for the next sweep."""
    profile_id = f"laptop-{uuid4().hex}"
    failing_id = uuid4().hex
    job_id = uuid4().hex
    await _prepared_invoice_without_flow(db_pool, profile_id, failing_id)
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    failing_name = f"{failing_id}.json"
    applied_name = f"{job_id}.json"
    drive = FakeDrive(
        {
            _path(failing_name): _sent_receipt(failing_id),
            _path(applied_name): _sent_receipt(job_id),
        }
    )

    with caplog.at_level(logging.ERROR, logger="invoicebot.pipeline.laptop"):
        await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        receipt = await _receipt_row(conn, job_id)
        failing_receipt = await _receipt_row(conn, failing_id)
    assert invoice == ("DONE", "BR-D10")
    assert receipt[0] is not None
    assert failing_receipt == (None, None)
    assert _path(failing_name) in drive.files
    assert _path(applied_name) not in drive.files
    assert failing_name in caplog.text


async def test_an_applied_receipt_is_deleted(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a receipt applied to a prepared batch is deleted after its transaction commits."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    await _prepared_invoice(db_pool, profile_id, job_id)
    name = f"{job_id}.json"
    drive = _drive(name, _sent_receipt(job_id))

    await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)

    assert drive.deleted == [_path(name)]
    assert drive.files == {}


async def test_a_forged_receipt_is_not_applied_nor_deleted(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a receipt signed with another key changes nothing and stays on the drive."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    name = f"{job_id}.json"
    drive = _drive(name, _forged_receipt(job_id))

    await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        receipt = await _receipt_row(conn, job_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "prepared"
    assert receipt == (None, None)
    assert drive.deleted == []
    assert _path(name) in drive.files


async def test_a_sent_receipt_on_a_sent_batch_stamps_the_job_and_queues_br_d10(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8, R-7.14: a late sent receipt on a sent batch only stamps the job and queues BR-D10."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    async with db_pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "sent")
    name = f"{job_id}.json"
    drive = _drive(name, _sent_receipt(job_id))

    await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        invoice = await _invoice_state(conn, invoice_id)
        batch_state = await get_batch_state(conn, invoice_id)
        jobs = await _jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        receipt = await _receipt_row(conn, job_id)
    assert invoice == ("BATCHED", "BR-D07")
    assert batch_state == "sent"
    assert jobs == []
    assert outbox == [(MESSAGES["BR-D10"].format(folder=FOLDER), "BR-D10", [])]
    assert receipt[0] is not None
    assert receipt[1] is None
    assert _path(name) not in drive.files


async def test_check_overdue_runs_after_a_sweep_with_a_failing_receipt(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-7.14: a raising receipt is caught, so the same sweep reaches check_overdue."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice_without_flow(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 25)
    name = f"{job_id}.json"
    drive = _drive(name, _sent_receipt(job_id))

    with caplog.at_level(logging.ERROR, logger="invoicebot.pipeline.laptop"):
        await poll_receipts(db_pool, drive, ROOT, KEY, LAPTOP_FLOWS)
        await check_overdue(db_pool, drive, ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        receipt = await _receipt_row(conn, job_id)
    assert outbox == [
        (
            MESSAGES["BR-D11"].format(serie=SERIE, numero=NUMERO),
            "BR-D11",
            [
                {"id": button_id(invoice_id, SELF_SEND_ACTION), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, ARRIVED_ACTION), "title": BUTTON_ARRIVED},
            ],
        ),
    ]
    assert receipt == (None, None)
    assert _path(name) in drive.files
    assert name in caplog.text


async def test_a_stale_job_in_the_outbox_gets_one_br_d12(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a job 5 h old whose file is still in _outbox gets one BR-D12 naming 5 hours."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 5)
    drive = FakeDrive({_job_file(job_id): b"{}"})

    await check_overdue(db_pool, drive, ROOT, Mail(), LAPTOP_FLOWS)
    await check_overdue(db_pool, drive, ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == [(MESSAGES["BR-D12"].format(serie=SERIE, numero=NUMERO, hours=5), "BR-D12", [])]


async def test_a_resend_job_on_a_sent_batch_gets_one_br_d12(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """D109: a resend job on a sent batch past agent_stale_after gets BR-D12 once."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 5)
    async with db_pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "sent")
    drive = FakeDrive({_job_file(job_id): b"{}"})

    await check_overdue(db_pool, drive, ROOT, Mail(), LAPTOP_FLOWS)
    await check_overdue(db_pool, drive, ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == [(MESSAGES["BR-D12"].format(serie=SERIE, numero=NUMERO, hours=5), "BR-D12", [])]


async def test_an_overdue_job_gets_one_br_d11(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: a job 25 h old with no receipt gets one BR-D11 and nothing on the next sweep."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 25)

    await check_overdue(db_pool, FakeDrive(), ROOT, Mail(), LAPTOP_FLOWS)
    await check_overdue(db_pool, FakeDrive(), ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == [
        (
            MESSAGES["BR-D11"].format(serie=SERIE, numero=NUMERO),
            "BR-D11",
            [
                {"id": button_id(invoice_id, VIA_LAPTOP_ACTION), "title": BUTTON_VIA_LAPTOP},
                {"id": button_id(invoice_id, SELF_SEND_ACTION), "title": BUTTON_SELF_SEND},
                {"id": button_id(invoice_id, ARRIVED_ACTION), "title": BUTTON_ARRIVED},
            ],
        ),
    ]


async def test_a_job_with_a_receipt_gets_no_alert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: a 25 h job whose receipt arrived gets neither alert."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 25)
    async with db_pool.connection() as conn, conn.transaction():
        await set_receipt_at(conn, job_id)

    await check_overdue(db_pool, FakeDrive({_job_file(job_id): b"{}"}), ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == []


async def test_a_picked_up_job_gets_no_br_d12(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.14: a 5 h job whose file the agent picked up gets no BR-D12."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 5)

    await check_overdue(db_pool, FakeDrive(), ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == []


async def test_a_job_whose_batch_is_sent_gets_neither_alert(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> None:
    """R-7.8: a 25 h job on a sent batch whose file the agent picked up gets neither alert."""
    profile_id = f"laptop-{uuid4().hex}"
    job_id = uuid4().hex
    invoice_id = await _prepared_invoice(db_pool, profile_id, job_id)
    await _age_job(db_pool, job_id, 25)
    async with db_pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "prepared")
        await set_batch_state(conn, batch_id, "sent")

    await check_overdue(db_pool, FakeDrive(), ROOT, Mail(), LAPTOP_FLOWS)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert outbox == []
