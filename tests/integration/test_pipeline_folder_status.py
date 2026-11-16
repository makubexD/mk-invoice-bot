"""Integration tests for the folder_status job handler (R-7.11).

The drive is an in-memory fake over a set of folder paths; Postgres is real. A folder
that Graph moved but the bot never recorded is found again, and a folder at neither its
recorded nor its target path is reported lost, never created.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, OneDriveFolderSink
from invoicebot.pipeline.folder_status import FolderLost, make_folder_status_handler
from invoicebot.pipeline.steps import FOLDER_STATUS_STEP
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.sinks.onedrive import FolderExists, FolderMissing, GraphTransient
from invoicebot.store.flow_b import (
    load_flow_data,
    merge_flow_data,
    open_batch,
    set_batch_state,
)
from invoicebot.store.invoices import create_invoice, set_flow

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
FLOW = "work_microsoft"
BASE = "2026-11-Support_7_LaLena"
YEAR = 2026
PAID_ON = date(2026, 12, 15)
PAYMENT_TOTAL = Decimal("144.60")
PAID_NAME = f"{BASE}_Sent_PAID_15_12___144_6"


def _parent(path: str) -> str | None:
    """Return the parent of an absolute drive path, or None for the root."""
    if path == "/":
        return None
    parts = path.rstrip("/").split("/")[1:]
    if not parts:
        return None
    return "/" if len(parts) == 1 else "/" + "/".join(parts[:-1])


def _name(path: str) -> str:
    """Return the last segment of an absolute drive path."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def _child(parent: str, name: str) -> str:
    """Join a parent path and a child name into an absolute drive path."""
    return f"{parent.rstrip('/')}/{name}"


class FakeDrive:
    """An in-memory Drive over a set of folder paths (R-7.11)."""

    def __init__(self, paths: set[str] | None = None, *, move_error: Exception | None = None) -> None:
        self.paths: set[str] = set(paths or ())
        self._move_error = move_error
        self.listings: list[str] = []
        self.created: list[tuple[str, str]] = []
        self.moves: list[tuple[str, str, str]] = []

    async def list_names(self, path: str) -> list[str]:
        """Return the direct child names of ``path``; a missing folder has none."""
        self.listings.append(path)
        return sorted(_name(p) for p in self.paths if _parent(p) == path)

    async def create_folder(self, parent: str, name: str) -> None:
        """Record the call and add the folder under its parent (idempotent)."""
        self.created.append((parent, name))
        self.paths.add(_child(parent, name))

    async def upload(self, path: str, data: bytes) -> None:
        """Not used by the folder_status job."""

    async def move(self, path: str, new_parent: str, new_name: str) -> None:
        """Move and rename the folder, or raise the Graph error its state calls for."""
        if self._move_error is not None:
            raise self._move_error
        if path not in self.paths:
            raise FolderMissing(404)
        target = _child(new_parent, new_name)
        if target in self.paths:
            raise FolderExists(409)
        self.paths = {
            target + p[len(path) :] if p == path or p.startswith(path + "/") else p for p in self.paths
        }
        self.moves.append((path, new_parent, new_name))


def _root(cfg: Config) -> str:
    """Return the flow's OneDrive folder sink root."""
    for sink in cfg.flows[FLOW].sinks:
        if isinstance(sink, OneDriveFolderSink):
            return sink.root
    raise AssertionError(f"{FLOW} has no folder sink")


def _config() -> Config:
    """Return the example config; the handler only reads the flow's sinks."""
    return load_config(EXAMPLE)


def _job(invoice_id: int) -> Job:
    """Build the folder_status job the handler receives."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=FOLDER_STATUS_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


def _handler(pool: AsyncConnectionPool, cfg: Config, drive: FakeDrive) -> Handler:
    """Build the folder_status handler."""
    return make_folder_status_handler(pool, cfg, drive)


async def _invoice(
    pool: AsyncConnectionPool,
    profile_id: str,
    *,
    folder: str,
    folder_path: str | None = None,
) -> int:
    """Create an invoice in FLOW with its folder and year flow data, and return its id."""
    data: dict[str, object] = {"folder": folder, "year": YEAR, "seq": 7}
    if folder_path is not None:
        data["folder_path"] = folder_path
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await set_flow(conn, invoice_id, FLOW)
        await merge_flow_data(conn, invoice_id, data)
    return invoice_id


async def _sent_batch(pool: AsyncConnectionPool, invoice_id: int) -> None:
    """Open a batch for the invoice and mark it sent (R-7.8)."""
    async with pool.connection() as conn, conn.transaction():
        batch_id = await open_batch(conn, invoice_id, FLOW, "sending")
        await set_batch_state(conn, batch_id, "sent")


async def _pay(
    pool: AsyncConnectionPool,
    invoice_id: int,
    profile_id: str,
    paid_on: date,
    amount: Decimal,
) -> None:
    """Store a payment and attach it to the invoice (R-7.12)."""
    async with pool.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO payments (flow_id, profile_id, paid_on, amount)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (FLOW, profile_id, paid_on, amount),
        )
        row = await cur.fetchone()
        assert row is not None
        await conn.execute("UPDATE invoices SET payment_id = %s WHERE id = %s", (row[0], invoice_id))


async def test_sent_without_payment_moves_the_folder_and_records_its_path(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a sent batch without a payment renames the folder to its ``_Sent`` name."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    drive = FakeDrive({f"{root}/{BASE}"})

    await _handler(db_pool, cfg, drive)(_job(invoice_id))

    target = f"{root}/{BASE}_Sent"
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert drive.moves == [(f"{root}/{BASE}", root, f"{BASE}_Sent")]
    assert drive.created == []
    assert flow_data["folder_path"] == target
    assert target in drive.paths


async def test_sent_and_paid_moves_into_the_year_folder_and_creates_it_once(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a sent and paid invoice moves into the year folder; a rerun changes nothing."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    await _pay(db_pool, invoice_id, profile_id, PAID_ON, PAYMENT_TOTAL)
    drive = FakeDrive({f"{root}/{BASE}"})
    handler = _handler(db_pool, cfg, drive)

    await handler(_job(invoice_id))
    await handler(_job(invoice_id))

    target = f"{root}/2026/{PAID_NAME}"
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert drive.moves == [(f"{root}/{BASE}", f"{root}/2026", PAID_NAME)]
    assert drive.created == [(root, "2026")]
    assert flow_data["folder_path"] == target
    assert target in drive.paths


async def test_a_second_run_after_the_sent_move_is_a_no_op(db_pool: AsyncConnectionPool) -> None:
    """R-7.11: once the folder path is the target, a rerun makes no further move."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    drive = FakeDrive({f"{root}/{BASE}"})
    handler = _handler(db_pool, cfg, drive)

    await handler(_job(invoice_id))
    await handler(_job(invoice_id))

    assert drive.moves == [(f"{root}/{BASE}", root, f"{BASE}_Sent")]
    assert drive.created == []


async def test_a_move_graph_applied_but_unrecorded_is_recorded_without_moving(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a folder already at the target is recorded there, with no successful move."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    target = f"{root}/{BASE}_Sent"
    drive = FakeDrive({target})

    await _handler(db_pool, cfg, drive)(_job(invoice_id))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert flow_data["folder_path"] == target
    assert drive.moves == []


async def test_paid_before_the_sent_rename_moves_once_to_the_final_name(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a payment before the ``_Sent`` rename still ends in one final-name move."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    await _pay(db_pool, invoice_id, profile_id, PAID_ON, PAYMENT_TOTAL)
    drive = FakeDrive({f"{root}/{BASE}"})

    await _handler(db_pool, cfg, drive)(_job(invoice_id))

    target = f"{root}/2026/{PAID_NAME}"
    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert drive.moves == [(f"{root}/{BASE}", f"{root}/2026", PAID_NAME)]
    assert drive.created == [(root, "2026")]
    assert flow_data["folder_path"] == target


async def test_source_and_target_missing_raises_folder_lost_without_creating(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a folder at neither path raises FolderLost and is never created."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    await _pay(db_pool, invoice_id, profile_id, PAID_ON, PAYMENT_TOTAL)
    drive = FakeDrive({f"{root}/2026"})

    with pytest.raises(FolderLost):
        await _handler(db_pool, cfg, drive)(_job(invoice_id))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert drive.created == []
    assert "folder_path" not in flow_data


async def test_a_transient_move_error_propagates_and_leaves_folder_path_unchanged(
    db_pool: AsyncConnectionPool,
) -> None:
    """R-7.11: a transient move failure propagates and the recorded path is unchanged."""
    profile_id = f"folder-{uuid4().hex}"
    cfg = _config()
    root = _root(cfg)
    invoice_id = await _invoice(db_pool, profile_id, folder=BASE)
    await _sent_batch(db_pool, invoice_id)
    drive = FakeDrive({f"{root}/{BASE}"}, move_error=GraphTransient(503))

    with pytest.raises(GraphTransient, match="status 503"):
        await _handler(db_pool, cfg, drive)(_job(invoice_id))

    async with db_pool.connection() as conn:
        flow_data = await load_flow_data(conn, invoice_id)
    assert "folder_path" not in flow_data
