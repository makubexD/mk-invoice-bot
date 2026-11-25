"""Integration tests for the ``portal_probe`` job that opens a taught link once (D193)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.fetchers.base import FetchedFile, FetchInvoice
from invoicebot.fetchers.browser.probe_protocol import ProbedField, ProbeOutcome, ProbeReport
from invoicebot.fetchers.portal_probe import Probe
from invoicebot.gaps.kinds import GAP_KINDS, fingerprint
from invoicebot.pipeline.portal_probe import (
    PROBE_RETRY_DELAYS,
    ProbeDeps,
    make_portal_probe_handler,
)
from invoicebot.pipeline.steps import DELIVER_STEP, PORTAL_PROBE_STEP
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job
from invoicebot.queue.worker import Handler
from invoicebot.states import CANCELLED, DELIVERING, FETCHING_DOCS, MANUAL_REQUIRED
from invoicebot.store import portal_probes, taught_links
from invoicebot.store.gaps import upsert_gap
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
#: From tests/synthetic_rucs.txt; no real RUC may appear here.
ISSUER_RUC = 20000000001
FLOW = "work_microsoft"
SENDER = "yo"
LINK_URL = "https://portal.example.pe/consulta"
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b"<Invoice><ID>F001-15160</ID></Invoice>"
GAP_FINGERPRINT = fingerprint("PORTAL_MISSING", str(ISSUER_RUC))
HELD = timedelta(hours=12)
SLACK_S = 120
_BYTES: dict[Literal["pdf", "xml"], bytes] = {"pdf": PDF_BYTES, "xml": XML_BYTES}
_FIELDS = (ProbedField(selector="#serie", kind="text", label="Serie", value_name="serie"),)


def _config(required_docs: Sequence[str] = ("pdf",)) -> Config:
    """Return the example config whose work flow requires ``required_docs``."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows[FLOW].model_copy(update={"required_docs": list(required_docs)})
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _reading() -> Reading:
    """Return a complete synthetic reading of the issuer's invoice."""
    values = {
        "ruc_emisor": str(ISSUER_RUC),
        "tipo": "01",
        "serie": "F001",
        "numero": "15160",
        "total": "49.90",
        "fecha_emision": "2026-11-13",
    }
    return Reading(
        fields={k: Field(value=v, source="qr", confidence=FULL_CONFIDENCE) for k, v in values.items()},
        document_url=None,
    )


def _pdf_reader(*, matching: bool) -> Callable[[Path], Awaitable[Reading | None]]:
    """Return a PDF reader that reads the invoice, or the same one with another total."""

    async def reader(_path: Path) -> Reading | None:
        reading = _reading()
        if matching:
            return reading
        other = {**reading.fields, "total": Field(value="10.00", source="ocr", confidence=FULL_CONFIDENCE)}
        return Reading(fields=other, document_url=None)

    return reader


def _written(out_dir: Path, kind: Literal["pdf", "xml"]) -> FetchedFile:
    """Write one small synthetic file into ``out_dir`` and return its reference."""
    path = out_dir / f"invoice.{kind}"
    data = _BYTES[kind]
    path.write_bytes(data)
    return FetchedFile(kind, path, len(data), hashlib.sha256(data).hexdigest())


class FakeProber:
    """A prober port that answers one outcome with the chosen files and runs a hook while it works."""

    def __init__(
        self,
        outcome: ProbeOutcome = ProbeOutcome.OK,
        kinds: tuple[Literal["pdf", "xml"], ...] = ("pdf",),
        *,
        with_fields: bool = True,
        during: Callable[[], Awaitable[None]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.kinds = kinds
        self.with_fields = with_fields
        self.during = during
        self.error = error
        self.urls: list[str] = []

    async def probe(self, url: str, invoice: FetchInvoice, out_dir: Path) -> Probe:
        """Record the URL, run the hook, write the files and return the report."""
        del invoice
        self.urls.append(url)
        if self.during is not None:
            await self.during()
        if self.error is not None:
            _written(out_dir, "pdf")
            raise self.error
        files = tuple(_written(out_dir, kind) for kind in self.kinds)
        report = ProbeReport(outcome=self.outcome, fields=_FIELDS if self.with_fields else ())
        return Probe(report, files)


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def clean_probe_tables(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from empty probes, links, gaps and outbox rows."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM portal_probes")
        await conn.execute("DELETE FROM taught_links")
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM outbox")
    yield


async def _world(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    *,
    state: str = MANUAL_REQUIRED,
    rule_id: str | None = "BR-F07",
) -> int:
    """Create a routed invoice, an approved link, a queued probe, the gap and its held issue row."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, SENDER, "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state, rule_id=rule_id)
        version = await taught_links.propose(conn, ISSUER_RUC, LINK_URL, SENDER)
        assert version is not None
        await taught_links.decide(conn, ISSUER_RUC, version, True)
        await portal_probes.start(conn, ISSUER_RUC, version, invoice_id)
        await upsert_gap(conn, GAP_KINDS["PORTAL_MISSING"], GAP_FINGERPRINT, f"RUC {ISSUER_RUC}", invoice_id)
        issue_id = await outbox.add(
            conn, "github", "owner/ops", {"op": "create_issue", "fingerprint": GAP_FINGERPRINT, "fields": {}}
        )
        await outbox.hold(conn, issue_id, HELD)
    return invoice_id


def _job(invoice_id: int, job_id: int = 1) -> Job:
    """Build the job the handler receives."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=PORTAL_PROBE_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    prober: FakeProber,
    data_dir: Path,
    *,
    cfg: Config | None = None,
    matching: bool = True,
) -> Handler:
    """Build the probe handler over a fake prober and PDF reader."""
    deps = ProbeDeps(pool, cfg or _config(), prober, _pdf_reader(matching=matching), data_dir)
    return make_portal_probe_handler(deps)


async def _rules(conn: AsyncConnection[Any]) -> list[str]:
    """Return the rule ids of the chat messages queued, oldest first."""
    cur = await conn.execute(
        "SELECT payload_json->>'rule_id' FROM outbox WHERE channel <> 'github' ORDER BY id"
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def _github_ops(conn: AsyncConnection[Any]) -> list[str]:
    """Return the ops of the GitHub rows queued, oldest first."""
    cur = await conn.execute("SELECT payload_json->>'op' FROM outbox WHERE channel = 'github' ORDER BY id")
    return [str(row[0]) for row in await cur.fetchall()]


async def _issue_held(conn: AsyncConnection[Any]) -> bool:
    """Return True when the create_issue row still waits for a later time."""
    cur = await conn.execute(
        "SELECT run_after > now() + interval '1 hour' FROM outbox WHERE payload_json->>'op' = 'create_issue'"
    )
    row = await cur.fetchone()
    assert row is not None
    return bool(row[0])


async def _state(conn: AsyncConnection[Any], invoice_id: int) -> str:
    cur = await conn.execute("SELECT state FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _jobs(conn: AsyncConnection[Any], invoice_id: int, step: str) -> list[float]:
    """Return the seconds until each ``step`` job of the invoice is due, oldest first."""
    cur = await conn.execute(
        "SELECT EXTRACT(EPOCH FROM run_after - now()) FROM jobs "
        "WHERE invoice_id = %s AND step = %s ORDER BY id",
        (invoice_id, step),
    )
    return [float(row[0]) for row in await cur.fetchall()]


async def _file_kinds(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    cur = await conn.execute("SELECT kind FROM files WHERE invoice_id = %s ORDER BY id", (invoice_id,))
    return [str(row[0]) for row in await cur.fetchall()]


def _leftovers(data_dir: Path, invoice_id: int) -> list[Path]:
    """Return every entry still under the invoice's temporary folder."""
    folder = data_dir / "tmp" / str(invoice_id)
    return sorted(folder.rglob("*")) if folder.exists() else []


async def test_matching_pdf_that_covers_the_flow_is_filed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: an ok probe whose PDF reads as the invoice and covers [pdf] files it for delivery."""
    invoice_id = await _world(db_pool)
    await _handler(db_pool, FakeProber(), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == DELIVERING
        assert len(await _jobs(conn, invoice_id, DELIVER_STEP)) == 1
        assert await _file_kinds(conn, invoice_id) == ["pdf"]
        assert await _rules(conn) == ["BR-S06_probe_filed"]
        assert await _github_ops(conn) == ["create_issue", "probe_report"]
        assert not await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert (probe.state, probe.outcome) == ("done", "ok")
    assert [path.name for path in _leftovers(tmp_path, invoice_id) if path.is_file()] == ["invoice.pdf"]


async def test_pdf_that_does_not_match_is_not_filed(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a lone PDF that reads as another invoice leaves the state alone and answers probe_form."""
    invoice_id = await _world(db_pool)
    await _handler(db_pool, FakeProber(), tmp_path, matching=False)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _jobs(conn, invoice_id, DELIVER_STEP) == []
        assert await _file_kinds(conn, invoice_id) == []
        assert await _rules(conn) == ["BR-S06_probe_form"]
        assert await _github_ops(conn) == ["create_issue", "probe_report"]
        assert not await _issue_held(conn)
    assert _leftovers(tmp_path, invoice_id) == []


async def test_pdf_alone_does_not_cover_a_flow_that_needs_xml_too(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a matching PDF with a flow that needs [xml, pdf] is not filed."""
    invoice_id = await _world(db_pool)
    await _handler(db_pool, FakeProber(), tmp_path, cfg=_config(("xml", "pdf")))(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _jobs(conn, invoice_id, DELIVER_STEP) == []
        assert await _rules(conn) == ["BR-S06_probe_form"]
        assert not await _issue_held(conn)
    assert _leftovers(tmp_path, invoice_id) == []


@pytest.mark.parametrize(
    "outcome", [ProbeOutcome.CAPTCHA, ProbeOutcome.UNMATCHED_FIELD, ProbeOutcome.LOGIN_ONLY]
)
async def test_a_stopped_probe_queues_its_report(
    outcome: ProbeOutcome, db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a captcha, an unmatched field or a login-only page ends the probe with a report on the issue."""
    invoice_id = await _world(db_pool)
    await _handler(db_pool, FakeProber(outcome, ()), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _rules(conn) == ["BR-S06_probe_form"]
        assert await _github_ops(conn) == ["create_issue", "probe_report"]
        assert not await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert (probe.state, probe.outcome) == ("done", outcome.value)
    assert probe.report is not None
    assert probe.report["files"] == []
    assert _leftovers(tmp_path, invoice_id) == []


async def test_busy_retries_after_15_minutes_1_hour_and_6_hours_then_fails_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a busy portal is tried again after 15 min, 1 h and 6 h, then ends with one probe_failed."""
    invoice_id = await _world(db_pool)
    handler = _handler(db_pool, FakeProber(ProbeOutcome.BUSY, (), with_fields=False), tmp_path)
    for attempt, delay in enumerate(PROBE_RETRY_DELAYS, start=1):
        await handler(_job(invoice_id, attempt))
        async with db_pool.connection() as conn:
            due = await _jobs(conn, invoice_id, PORTAL_PROBE_STEP)
            assert len(due) == attempt
            assert delay.total_seconds() - SLACK_S < due[-1] <= delay.total_seconds()
            assert await _rules(conn) == []
            assert await _issue_held(conn)
            probe = await portal_probes.get(conn, ISSUER_RUC)
        assert probe is not None
        assert (probe.state, probe.tries) == ("queued", attempt)
    await handler(_job(invoice_id, len(PROBE_RETRY_DELAYS) + 1))
    async with db_pool.connection() as conn:
        assert len(await _jobs(conn, invoice_id, PORTAL_PROBE_STEP)) == len(PROBE_RETRY_DELAYS)
        assert await _rules(conn) == ["BR-S06_probe_failed"]
        assert await _github_ops(conn) == ["create_issue", "probe_report"]
        assert not await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert (probe.state, probe.outcome) == ("done", "busy")
    assert _leftovers(tmp_path, invoice_id) == []


async def test_cancelled_while_probing_files_nothing_and_ends_the_probe(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: an invoice cancelled during the probe gets no file, and the probe ends and releases the issue."""
    invoice_id = await _world(db_pool)

    async def cancel() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await set_state(conn, invoice_id, CANCELLED, rule_id="BR-D06")

    await _handler(db_pool, FakeProber(during=cancel), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == CANCELLED
        assert await _jobs(conn, invoice_id, DELIVER_STEP) == []
        assert await _file_kinds(conn, invoice_id) == []
        assert not await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert probe.state == "done"
    assert _leftovers(tmp_path, invoice_id) == []


async def test_invoice_already_delivering_from_another_fetcher_gets_no_file(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: an invoice another fetcher moved to DELIVERING during the probe is not filed again."""
    invoice_id = await _world(db_pool)

    async def deliver_elsewhere() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await set_state(conn, invoice_id, DELIVERING)

    await _handler(db_pool, FakeProber(during=deliver_elsewhere), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == DELIVERING
        assert await _jobs(conn, invoice_id, DELIVER_STEP) == []
        assert await _file_kinds(conn, invoice_id) == []
        assert "BR-S06_probe_filed" not in await _rules(conn)
        assert not await _issue_held(conn)
    assert _leftovers(tmp_path, invoice_id) == []


async def test_invoice_that_left_before_the_probe_ends_it_without_opening_the_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: an invoice already out of the waiting states ends its probe as technical and probes nothing."""
    invoice_id = await _world(db_pool, state=DELIVERING, rule_id=None)
    prober = FakeProber()
    await _handler(db_pool, prober, tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _rules(conn) == []
        assert await _github_ops(conn) == ["create_issue"]
        assert not await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert (probe.state, probe.outcome) == ("done", "technical")
    assert prober.urls == []
    assert _leftovers(tmp_path, invoice_id) == []


async def test_link_replaced_while_probing_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a probe whose link version moved on writes no file, message, report or release."""
    invoice_id = await _world(db_pool)

    async def replace_link() -> None:
        async with db_pool.connection() as conn, conn.transaction():
            await portal_probes.start(conn, ISSUER_RUC, 2, invoice_id)

    await _handler(db_pool, FakeProber(during=replace_link), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == MANUAL_REQUIRED
        assert await _rules(conn) == []
        assert await _github_ops(conn) == ["create_issue"]
        assert await _issue_held(conn)
        assert await _jobs(conn, invoice_id, DELIVER_STEP) == []
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert (probe.state, probe.link_version, probe.tries) == ("queued", 2, 0)
    assert _leftovers(tmp_path, invoice_id) == []


async def test_job_run_twice_gives_one_message_and_one_deliver_job(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: two runs of the same job, one after the other or together, file the invoice once."""
    invoice_id = await _world(db_pool)
    handler = _handler(db_pool, FakeProber(), tmp_path)
    await asyncio.gather(handler(_job(invoice_id, 1)), handler(_job(invoice_id, 1)))
    await handler(_job(invoice_id, 1))
    async with db_pool.connection() as conn:
        assert await _rules(conn) == ["BR-S06_probe_filed"]
        assert len(await _jobs(conn, invoice_id, DELIVER_STEP)) == 1
        assert await _file_kinds(conn, invoice_id) == ["pdf"]
        assert await _github_ops(conn) == ["create_issue", "probe_report"]
    assert len([path for path in _leftovers(tmp_path, invoice_id) if path.is_file()]) == 1


async def test_job_without_a_queued_probe_ends_quietly(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: an invoice with no queued probe is left alone and nothing is probed."""
    invoice_id = await _world(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM portal_probes")
    prober = FakeProber()
    await _handler(db_pool, prober, tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _rules(conn) == []
        assert await _issue_held(conn)
    assert prober.urls == []
    assert _leftovers(tmp_path, invoice_id) == []


async def test_prober_exception_deletes_the_folder_and_is_raised(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a prober that raises leaves no file behind and the error reaches the queue."""
    invoice_id = await _world(db_pool)
    prober = FakeProber(error=RuntimeError("synthetic probe failure"))
    with pytest.raises(RuntimeError, match="synthetic probe failure"):
        await _handler(db_pool, prober, tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _rules(conn) == []
        assert await _issue_held(conn)
        probe = await portal_probes.get(conn, ISSUER_RUC)
    assert probe is not None
    assert probe.state == "queued"
    assert _leftovers(tmp_path, invoice_id) == []


async def test_probe_uses_the_approved_link_without_a_session(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: the prober gets the approved URL with any session address removed."""
    invoice_id = await _world(db_pool)
    async with db_pool.connection() as conn:
        await conn.execute(
            "UPDATE taught_links SET url = %s WHERE ruc_emisor = %s",
            ("https://portal.example.pe/(S(abc123))/consulta", ISSUER_RUC),
        )
    prober = FakeProber(ProbeOutcome.CAPTCHA, ())
    await _handler(db_pool, prober, tmp_path)(_job(invoice_id))
    assert prober.urls == ["https://portal.example.pe/consulta"]


async def test_fetching_docs_invoice_is_filed_like_a_manual_one(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D193: a probe for an invoice still in FETCHING_DOCS files it like one in MANUAL_REQUIRED."""
    invoice_id = await _world(db_pool, state=FETCHING_DOCS, rule_id=None)
    await _handler(db_pool, FakeProber(), tmp_path)(_job(invoice_id))
    async with db_pool.connection() as conn:
        assert await _state(conn, invoice_id) == DELIVERING
