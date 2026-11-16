"""Integration tests for the go-live claim import (R-7.16, D110).

Each test imports synthetic claim folders under ``tmp_path`` into the real database and
uses its own profile id, so tests cannot see each other's invoices.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import ChannelName
from invoicebot.importer import Claim, ImportReport, read_claims, register_claims
from invoicebot.states import DONE
from invoicebot.store.flow_b import claim_seq
from invoicebot.store.invoices import create_invoice, set_flow
from invoicebot.store.payments import candidates

# The existing synthetic invoice generator; its RUCs are the ones in tests/synthetic_rucs.txt.
from unit.test_extract_ubl import _invoice_xml

pytestmark = pytest.mark.asyncio(loop_scope="session")

CHANNEL: ChannelName = "whatsapp"
YEAR = 2026
MONTH = 7
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"


def _unique(prefix: str) -> str:
    """Return an id no other test uses, so tests cannot interfere."""
    return f"{prefix}-{uuid4().hex}"


def _claim_folder(root: Path, name: str, numero: str) -> Path:
    """Create one sent claim folder with its synthetic XML under root."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / "invoice.xml").write_text(_invoice_xml(numero=numero), encoding="utf-8")
    return folder


def _claims(root: Path) -> list[Claim]:
    """Return the claims read under root, in walk order."""
    return [reading.result for reading in read_claims([root]) if isinstance(reading.result, Claim)]


async def _register(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    flow_id: str,
    claims: list[Claim],
    *,
    dry_run: bool = False,
) -> ImportReport:
    """Run the import inside a transaction the test owns."""
    async with db_pool.connection() as conn, conn.transaction():
        return await register_claims(conn, profile_id, flow_id, claims, dry_run=dry_run)


def _two_claims(root: Path) -> list[Claim]:
    """Build a root claim folder and a claim in the year folder, and return both claims."""
    root.mkdir()
    _claim_folder(root, "2026-07-Support_3_LaLena_Sent", "17101")
    _claim_folder(root / str(YEAR), "2026-07-Support_4_LaLena_Sent", "17102")
    return _claims(root)


async def test_register_claims_files_sent_claims_done_with_their_seq(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.16: two sent claims become DONE flow B invoices with a sent batch and their seq."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    claims = _two_claims(tmp_path / "Reembolsos")

    report = await _register(db_pool, profile_id, flow_id, claims)

    assert sorted(entry.status for entry in report.entries) == ["registered", "registered"]
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT i.id, i.state, i.flow_id, i.natural_key, i.ruc_emisor, i.total,
                   i.fields_json, i.flow_data_json, b.state, i.numero
            FROM invoices AS i
            JOIN batches AS b ON b.id = i.batch_id
            WHERE i.profile_id = %s
            ORDER BY i.id
            """,
            (profile_id,),
        )
        rows = await cur.fetchall()

    assert len(rows) == 2
    seqs = {row[7]["seq"]: row for row in rows}
    assert set(seqs) == {3, 4}
    for seq, row in seqs.items():
        assert row[1] == DONE
        assert row[2] == flow_id
        assert row[8] == "sent"
        assert row[7]["imported"] is True
        assert row[7]["year"] == YEAR
        assert row[7]["month"] == MONTH
        assert row[7]["comercio"] == "LaLena"
        assert row[7]["seq"] == seq
        assert row[7]["folder"] == f"2026-07-Support_{seq}_LaLena"
        assert row[7]["folder_path"].endswith(f"2026-07-Support_{seq}_LaLena_Sent")
        assert row[3] == f"{profile_id}|{SYNTHETIC_RUC}|01|F014|{row[9]}"
        assert row[4] == int(SYNTHETIC_RUC)
        assert row[5] == Decimal("49.90")
        assert row[6]["fields"]["total"]["value"] == "49.90"


async def test_register_claims_second_run_registers_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.16: a second run skips both claims and writes no invoice."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    claims = _two_claims(tmp_path / "Reembolsos")
    await _register(db_pool, profile_id, flow_id, claims)

    report = await _register(db_pool, profile_id, flow_id, claims)

    assert [entry.status for entry in report.entries] == ["skipped", "skipped"]
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM invoices WHERE profile_id = %s", (profile_id,))
        row = await cur.fetchone()
    assert row is not None
    assert int(row[0]) == 2


async def test_register_claims_dry_run_writes_no_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.16: a dry run reports the claims it would register and writes no invoice."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    claims = _two_claims(tmp_path / "Reembolsos")

    report = await _register(db_pool, profile_id, flow_id, claims, dry_run=True)

    assert [entry.status for entry in report.entries] == ["would_register", "would_register"]
    async with db_pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM invoices WHERE profile_id = %s", (profile_id,))
        row = await cur.fetchone()
    assert row is not None
    assert int(row[0]) == 0


async def test_claim_seq_continues_after_an_imported_seq(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.1: after an imported seq 3, that month's next claimed number is 4."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    root = tmp_path / "Reembolsos"
    root.mkdir()
    _claim_folder(root, "2026-07-Support_3_LaLena_Sent", "17103")
    await _register(db_pool, profile_id, flow_id, _claims(root))

    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, CHANNEL)
        await set_flow(conn, invoice_id, flow_id)
        seq = await claim_seq(conn, flow_id, YEAR, MONTH, listed_max=0, invoice_id=invoice_id)

    assert seq == 4


async def test_imported_invoice_is_a_pagado_candidate(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.12: an imported DONE sent invoice is a candidate for ``/pagado``."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    root = tmp_path / "Reembolsos"
    root.mkdir()
    _claim_folder(root, "2026-07-Support_3_LaLena_Sent", "17104")
    report = await _register(db_pool, profile_id, flow_id, _claims(root))
    imported_id = report.entries[0].invoice_id

    async with db_pool.connection() as conn:
        found = await candidates(conn, profile_id, flow_id, None)

    assert [candidate.invoice_id for candidate in found] == [imported_id]
    assert found[0].claimed_cents == 4990


async def test_register_claims_leaves_the_folder_tree_untouched(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-7.16: a run reads the folders and changes no file or mtime."""
    profile_id = _unique("profile")
    flow_id = _unique("flow")
    root = tmp_path / "Reembolsos"
    claims = _two_claims(root)
    before = _tree(root)

    await _register(db_pool, profile_id, flow_id, claims)

    assert _tree(root) == before


def _tree(root: Path) -> dict[str, tuple[bool, int, int | None]]:
    """Return every path under root with its kind, mtime and, for a file, its size."""
    return {
        str(path.relative_to(root)): (
            path.is_dir(),
            path.stat().st_mtime_ns,
            None if path.is_dir() else path.stat().st_size,
        )
        for path in root.rglob("*")
    }
