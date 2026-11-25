"""Integration tests for the fetch job handler (S7.1, R-5.1, R-5.7, D65, BR-F07)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import (
    DEFAULT_RETRY_SCHEDULE,
    EMAIL_INBOX_FETCHER,
    Brand,
    Config,
    Flow,
    Portal,
    Store,
    StoreDirectory,
)
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.base import FetchedFile, Fetcher, FetchInvoice, FetchReason, FetchResult
from invoicebot.pipeline.fetch import FETCH_STEP, PORTAL_FETCHER, Schedule, make_fetch_handler
from invoicebot.pipeline.manual_message import portal_for
from invoicebot.pipeline.questions import (
    RULE_DOCS_NEEDED,
    SEND_PDF_ACTION,
    TEACH_PORTAL_ACTION,
)
from invoicebot.pipeline.steps import DELIVER_STEP
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler
from invoicebot.rules.messages_es import (
    BUTTON_DONT_KNOW,
    BUTTON_WRITE_PORTAL,
    FETCH_NO_PORTAL_LINE,
    FETCH_TECHNICAL_LINE,
    FETCH_WAITING,
    GAP_LINE_PORTAL,
    MESSAGES,
    fetcher_desc,
    when_desc,
)
from invoicebot.states import StateMoved
from invoicebot.store.fetch_attempts import record_attempt
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
# A synthetic issuer the store directory does not know.
UNKNOWN_RUC = "20000000010"
PORTAL_KEY = "synthetic_portal"
FLOW = "work_microsoft"
PROFILE = "yo"
SERIE = "F001"
NUMERO = "15160"
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b"<Invoice><ID>F001-15160</ID></Invoice>"
MINUTES_55 = 55
HOURS_5_5 = 5.5
HOURS_6_5 = 6.5
MINUTES_65 = 65
MINUTES_42 = 42
_FILE_BYTES: dict[Literal["pdf", "xml"], bytes] = {"pdf": PDF_BYTES, "xml": XML_BYTES}


def _config(
    fetchers: Sequence[str] = ("portal_registry",), *, required_docs: Sequence[str] = ("pdf", "xml")
) -> Config:
    """Return the example config with the work flow using ``fetchers`` and requiring ``required_docs``."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows[FLOW].model_copy(
        update={"fetchers": list(fetchers), "required_docs": list(required_docs)}
    )
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _directory() -> StoreDirectory:
    """Return a store directory with one store whose active portal retries in 1 h and 6 h."""
    portal = Portal(
        key=PORTAL_KEY,
        adapter="generic_form",
        status="active",
        retry_schedule=["1h", "6h"],
    )
    store = Store(
        ruc=STORE_RUC,
        razon_social="Comercio Sintético",
        tipo="01",
        brands=[Brand(comercio="Tienda Sintética")],
        portal=PORTAL_KEY,
    )
    return StoreDirectory(stores={STORE_RUC: store}, portals={PORTAL_KEY: portal})


def _inbox_schedule(flow: Flow, _invoice: FetchInvoice) -> tuple[timedelta, ...]:
    """Return the app's inbox entry: the flow's inbox schedule, or the default (D65, D98)."""
    inbox = flow.email_inbox
    return DEFAULT_RETRY_SCHEDULE if inbox is None else inbox.retry_schedule


def _portal_schedule(directory: StoreDirectory) -> Schedule:
    """Return the app's portal entry over ``directory``: the store's portal schedule or the default."""

    def schedule(_flow: Flow, invoice: FetchInvoice) -> tuple[timedelta, ...]:
        portal = portal_for(invoice.ruc_emisor, directory)
        return DEFAULT_RETRY_SCHEDULE if portal is None else portal.retry_schedule

    return schedule


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a complete synthetic reading the fetcher needs."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field("01"),
            "serie": _field(SERIE),
            "numero": _field(NUMERO),
            "total": _field("49.90"),
            "fecha_emision": _field("2026-11-13"),
        },
        document_url=None,
    )


def _reading_for(ruc_emisor: str) -> Reading:
    """Build the complete synthetic reading with ``ruc_emisor`` as its issuer."""
    fields = dict(_reading().fields)
    fields["ruc_emisor"] = _field(ruc_emisor)
    return Reading(fields=fields, document_url=None)


def _written(out_dir: Path, kind: Literal["pdf", "xml"], data: bytes) -> FetchedFile:
    """Write one small synthetic file into ``out_dir`` and return its reference."""
    path = out_dir / f"invoice.{kind}"
    path.write_bytes(data)
    return FetchedFile(
        kind=kind,
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


class FakeFetcher:
    """A fetcher that writes ``kinds`` on ``ok``, a partial one otherwise, and returns its reason."""

    def __init__(
        self,
        reason: FetchReason,
        kinds: tuple[Literal["pdf", "xml"], ...] = ("pdf", "xml"),
    ) -> None:
        self.reason = reason
        self.kinds = kinds
        self.calls = 0
        self.invoices: list[FetchInvoice] = []

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        self.calls += 1
        self.invoices.append(invoice)
        if self.reason is not FetchReason.OK:
            _written(out_dir, "pdf", PDF_BYTES)
            return FetchResult(self.reason, (), PORTAL_KEY)
        files = tuple(_written(out_dir, kind, _FILE_BYTES[kind]) for kind in self.kinds)
        return FetchResult(FetchReason.OK, files, PORTAL_KEY)


class LeavingFetcher(FakeFetcher):
    """An ok fetcher that moves the invoice out of FETCHING_DOCS before the handler commits."""

    def __init__(
        self,
        pool: AsyncConnectionPool[AsyncConnection[Any]],
        invoice_id: int,
    ) -> None:
        super().__init__(FetchReason.OK)
        self._pool = pool
        self._invoice_id = invoice_id

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        result = await super().fetch(invoice, out_dir)
        async with self._pool.connection() as conn, conn.transaction():
            await set_state(conn, self._invoice_id, "DELIVERING")
        return result


class CancellingFetcher(FakeFetcher):
    """An ok fetcher that cancels the invoice before the handler commits (D95)."""

    def __init__(
        self,
        pool: AsyncConnectionPool[AsyncConnection[Any]],
        invoice_id: int,
    ) -> None:
        super().__init__(FetchReason.OK)
        self._pool = pool
        self._invoice_id = invoice_id

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        result = await super().fetch(invoice, out_dir)
        async with self._pool.connection() as conn, conn.transaction():
            await set_state(conn, self._invoice_id, "CANCELLED", rule_id="BR-S07")
        return result


class RecordingFetcher:
    """A fetcher that records the folder each run gives it, then fails."""

    def __init__(self) -> None:
        self.out_dirs: list[Path] = []

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        del invoice
        self.out_dirs.append(out_dir)
        raise RuntimeError("synthetic fetch failure")


def _all_exist(paths: Sequence[str]) -> bool:
    """Return True when every stored path still exists on disk."""
    return all(Path(path).exists() for path in paths)


class _CancellingPool:
    """A pool whose ``cancel_on``-th connection raises CancelledError after its inner context (R-5.1)."""

    def __init__(self, pool: AsyncConnectionPool[AsyncConnection[Any]], cancel_on: int) -> None:
        self._pool = pool
        self._cancel_on = cancel_on
        self.calls = 0

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncConnection[Any]]:
        """Yield a real pooled connection; the final one raises once the transaction has exited."""
        self.calls += 1
        cancels = self.calls == self._cancel_on
        async with self._pool.connection() as conn:
            yield conn
        if cancels:
            raise asyncio.CancelledError


async def _fetching_invoice(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    profile_id: str,
    *,
    reading: Reading | None = None,
    state: str = "FETCHING_DOCS",
) -> int:
    """Create a routed invoice with a stored reading and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await save_reading(conn, invoice_id, reading or _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, state)
    return invoice_id


def _job(invoice_id: int, job_id: int = 1) -> Job:
    """Build the job the handler receives for ``invoice_id``."""
    return Job(
        id=job_id,
        invoice_id=invoice_id,
        step=FETCH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


def _handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    fetcher: Fetcher,
    data_dir: Path,
    cfg: Config | None = None,
) -> Handler:
    """Build the fetch handler with the one-fetcher registry and the portal schedule."""
    directory = _directory()
    return make_fetch_handler(
        pool,
        cfg or _config(),
        directory,
        {FLOW: {"portal_registry": fetcher}},
        data_dir,
        schedules={PORTAL_FETCHER: _portal_schedule(directory)},
    )


async def _run(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    fetcher: Fetcher,
    invoice_id: int,
    data_dir: Path,
    job_id: int = 1,
    cfg: Config | None = None,
) -> None:
    """Run the fetch handler once with the given job id."""
    await _handler(pool, fetcher, data_dir, cfg)(_job(invoice_id, job_id))


async def _invoice(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None, str | None]:
    cur = await conn.execute("SELECT state, rule_id, flow_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1], row[2]


async def _files(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[str, str, str, int]]:
    cur = await conn.execute(
        "SELECT kind, local_path, sha256, size FROM files WHERE invoice_id = %s ORDER BY id",
        (invoice_id,),
    )
    return [(str(kind), str(path), str(sha), int(size)) for kind, path, sha, size in await cur.fetchall()]


async def _attempts(conn: AsyncConnection[Any], invoice_id: int) -> list[tuple[str, str | None, bool, str]]:
    cur = await conn.execute(
        """
        SELECT fetcher, portal_key, ok, reason
        FROM fetch_attempts
        WHERE invoice_id = %s
        ORDER BY id
        """,
        (invoice_id,),
    )
    return [(str(fetcher), key, bool(ok), str(reason)) for fetcher, key, ok, reason in await cur.fetchall()]


async def _fetch_jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[Any]:
    cur = await conn.execute(
        "SELECT run_after FROM jobs WHERE invoice_id = %s AND step = %s ORDER BY id",
        (invoice_id, FETCH_STEP),
    )
    return [row[0] for row in await cur.fetchall()]


async def _deliver_jobs(conn: AsyncConnection[Any], invoice_id: int) -> list[Any]:
    cur = await conn.execute(
        "SELECT run_after FROM jobs WHERE invoice_id = %s AND step = %s ORDER BY id",
        (invoice_id, DELIVER_STEP),
    )
    return [row[0] for row in await cur.fetchall()]


async def _latest_fetch_job_id(conn: AsyncConnection[Any], invoice_id: int) -> int:
    cur = await conn.execute(
        "SELECT id FROM jobs WHERE invoice_id = %s AND step = %s ORDER BY id DESC LIMIT 1",
        (invoice_id, FETCH_STEP),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _latest_delay(conn: AsyncConnection[Any], invoice_id: int) -> timedelta:
    """Return the furthest fetch job's ``run_after`` relative to the database clock."""
    cur = await conn.execute(
        "SELECT max(run_after) - now() FROM jobs WHERE invoice_id = %s AND step = %s",
        (invoice_id, FETCH_STEP),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] is not None
    return row[0]


async def _outbox(conn: AsyncConnection[Any], profile_id: str) -> list[tuple[str, str | None]]:
    cur = await conn.execute(
        """
        SELECT payload_json->>'text', payload_json->>'rule_id'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [(str(text), rule_id) for text, rule_id in await cur.fetchall()]


async def _outbox_buttons(conn: AsyncConnection[Any], profile_id: str) -> list[list[str]]:
    cur = await conn.execute(
        """
        SELECT payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [[str(button["id"]) for button in buttons] for (buttons,) in await cur.fetchall()]


async def _outbox_button_dicts(conn: AsyncConnection[Any], profile_id: str) -> list[list[dict[str, str]]]:
    """Return each outbox row's buttons as their wire dicts, oldest first."""
    cur = await conn.execute(
        """
        SELECT payload_json->'buttons'
        FROM outbox
        WHERE "to" = %s
        ORDER BY id
        """,
        (profile_id,),
    )
    return [cast(list[dict[str, str]], buttons) for (buttons,) in await cur.fetchall()]


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _clean_gap_state(db_pool: AsyncConnectionPool[AsyncConnection[Any]]) -> AsyncIterator[None]:
    """Start each test from empty gaps and invoice links, so one test's raise cannot leak."""
    async with db_pool.connection() as conn:
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM taught_links")
    yield


async def _gaps(conn: AsyncConnection[Any]) -> list[tuple[str, str]]:
    """Return each gap's fingerprint and kind."""
    cur = await conn.execute("SELECT fingerprint, kind FROM gaps ORDER BY fingerprint")
    return [(str(fingerprint), str(kind)) for fingerprint, kind in await cur.fetchall()]


async def _gap_links(conn: AsyncConnection[Any], invoice_id: int) -> list[str]:
    """Return the fingerprints of the gaps linked to ``invoice_id``."""
    cur = await conn.execute(
        "SELECT fingerprint FROM invoice_gaps WHERE invoice_id = %s ORDER BY fingerprint",
        (invoice_id,),
    )
    return [str(row[0]) for row in await cur.fetchall()]


async def _seed_attempts(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    reason: str,
    count: int,
) -> None:
    async with pool.connection() as conn, conn.transaction():
        for _ in range(count):
            await record_attempt(conn, invoice_id, "portal_registry", PORTAL_KEY, reason, 1)


async def test_ok_stores_both_files_and_moves_to_delivering(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1, R-5.7: an ok fetch stores both files, records one attempt and enqueues deliver."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert invoice == ("DELIVERING", None, FLOW)
    assert [(kind, sha, size) for kind, _path, sha, size in files] == [
        ("pdf", hashlib.sha256(PDF_BYTES).hexdigest(), len(PDF_BYTES)),
        ("xml", hashlib.sha256(XML_BYTES).hexdigest(), len(XML_BYTES)),
    ]
    out_dir = tmp_path / "tmp" / str(invoice_id)
    assert all(Path(path).is_relative_to(out_dir) for _kind, path, _sha, _size in files)
    assert attempts == [("portal_registry", PORTAL_KEY, True, "ok")]
    assert jobs == []
    assert len(deliver_jobs) == 1


async def test_timeout_result_leaves_no_fetch_folder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.2: a fetch that ends in timeout leaves no folder under tmp/."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)

    await _run(db_pool, FakeFetcher(FetchReason.TIMEOUT), invoice_id, tmp_path)

    out_root = tmp_path / "tmp" / str(invoice_id)
    assert not out_root.exists() or not any(out_root.rglob("*"))


async def test_ok_result_keeps_its_files(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.2: an ok fetch keeps its files under tmp/ for deliver to move."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)

    await _run(db_pool, FakeFetcher(FetchReason.OK), invoice_id, tmp_path)

    (out_dir,) = (tmp_path / "tmp" / str(invoice_id)).iterdir()
    assert sorted(path.name for path in out_dir.iterdir()) == ["invoice.pdf", "invoice.xml"]


async def test_ok_without_a_required_document_records_missing_docs_and_chains(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1: an ok fetch lacking a required document records missing_docs and runs the next fetcher."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    first = FakeFetcher(FetchReason.OK, kinds=("pdf",))
    second = FakeFetcher(FetchReason.TECHNICAL)
    handler = make_fetch_handler(
        db_pool,
        _config(("portal_registry", "manual")),
        _directory(),
        {FLOW: {"portal_registry": first, "manual": second}},
        tmp_path,
    )

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)
    assert invoice == ("FETCHING_DOCS", None, FLOW)
    assert files == []
    assert attempts == [("portal_registry", PORTAL_KEY, False, "missing_docs")]
    assert len(jobs) == 1  # the next fetch job runs at once, so the chain moves on
    out_root = tmp_path / "tmp" / str(invoice_id)
    assert not out_root.exists() or not any(out_root.rglob("*"))

    await handler(_job(invoice_id, next_job_id))

    assert (first.calls, second.calls) == (1, 1)


async def test_not_found_yet_schedules_one_retry_and_one_status_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D65, R-5.7: a not-found-yet records the attempt, retries in about 1 h and says so once."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        cur = await conn.execute(
            "SELECT run_after - now() FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, FETCH_STEP),
        )
        delays = [row[0] for row in await cur.fetchall()]
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("FETCHING_DOCS", None, FLOW)
    assert attempts == [("portal_registry", PORTAL_KEY, False, "not_found_yet")]
    assert len(delays) == 1
    assert MINUTES_55 <= delays[0].total_seconds() / 60 <= MINUTES_65
    assert outbox == [(FETCH_WAITING.format(id=invoice_id), None)]


async def test_not_found_yet_with_an_empty_retry_schedule_schedules_at_once_and_says_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D65: an empty retry_schedule enqueues the next job at once and sends no FETCH_WAITING message."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    directory = _directory()
    portal = directory.portals[PORTAL_KEY].model_copy(update={"retry_schedule": ()})
    directory = directory.model_copy(update={"portals": {PORTAL_KEY: portal}})
    handler = make_fetch_handler(
        db_pool,
        _config(),
        directory,
        {FLOW: {"portal_registry": fetcher}},
        tmp_path,
        schedules={PORTAL_FETCHER: _portal_schedule(directory)},
    )

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert attempts == [("portal_registry", PORTAL_KEY, False, "not_found_yet")]
    assert len(jobs) == 1  # the next job is due at once, so the chain moves on
    assert abs((jobs[0] - datetime.now(UTC)).total_seconds()) < 10
    assert outbox == []


async def test_fetcher_without_a_schedule_entry_retries_on_the_default_schedule(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.2: a fetcher with no entry in ``schedules`` retries on DEFAULT_RETRY_SCHEDULE."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = make_fetch_handler(
        db_pool,
        _config(("made_up_fetcher",)),
        _directory(),
        {FLOW: {"made_up_fetcher": fetcher}},
        tmp_path,
        schedules={},
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
        first_delay = await _latest_delay(conn, invoice_id)

    await handler(_job(invoice_id, retry_job_id))
    async with db_pool.connection() as conn:
        second_delay = await _latest_delay(conn, invoice_id)

    assert MINUTES_55 <= first_delay.total_seconds() / 60 <= MINUTES_65
    assert HOURS_5_5 <= second_delay.total_seconds() / 3600 <= HOURS_6_5


async def test_schedule_entry_for_a_made_up_fetcher_sets_its_retry_delay(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.2: an entry for a made-up fetcher name is used for that fetcher."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = make_fetch_handler(
        db_pool,
        _config(("made_up_fetcher",)),
        _directory(),
        {FLOW: {"made_up_fetcher": fetcher}},
        tmp_path,
        schedules={"made_up_fetcher": lambda _flow, _invoice: (timedelta(minutes=MINUTES_42),)},
    )

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        delay = await _latest_delay(conn, invoice_id)
    assert abs(delay.total_seconds() - MINUTES_42 * 60) < 60


async def test_second_not_found_yet_retries_in_6h_without_a_second_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D65, R-5.1: the retry job runs the portal again; a second not-found-yet waits 6 h, no new message."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    await _run(db_pool, fetcher, invoice_id, tmp_path)
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)

    await _run(db_pool, fetcher, invoice_id, tmp_path, retry_job_id)

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
        cur = await conn.execute(
            "SELECT max(run_after) - now() FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, FETCH_STEP),
        )
        row = await cur.fetchone()
    assert fetcher.calls == 2
    assert len(attempts) == 2
    assert len(outbox) == 1
    assert row is not None
    assert HOURS_5_5 <= row[0].total_seconds() / 3600 <= HOURS_6_5


async def test_running_the_same_fetch_job_twice_fetches_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1: a redelivered job is superseded, so it fetches nothing and records no attempt."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        job_id = await enqueue(conn, invoice_id, FETCH_STEP)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = _handler(db_pool, fetcher, tmp_path)
    job = _job(invoice_id, job_id)

    await handler(job)
    await handler(job)

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
    assert fetcher.calls == 1
    assert attempts == [("portal_registry", PORTAL_KEY, False, "not_found_yet")]
    assert len(jobs) == 2  # the original job and the one retry the first run scheduled


async def test_second_run_of_the_same_ok_job_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1: a second run of an ok job records no attempt and no file, and leaves DELIVERING."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.OK)
    handler = _handler(db_pool, fetcher, tmp_path)
    job = _job(invoice_id)

    await handler(job)
    await handler(job)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
    assert invoice == ("DELIVERING", None, FLOW)
    assert attempts == [("portal_registry", PORTAL_KEY, True, "ok")]
    assert [kind for kind, _path, _sha, _size in files] == ["pdf", "xml"]
    assert fetcher.calls == 1


async def test_run_that_leaves_fetching_after_its_fetch_removes_its_folder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.2: a run whose state changed during the fetch writes nothing and leaves no files."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = LeavingFetcher(db_pool, invoice_id)
    handler = _handler(db_pool, fetcher, tmp_path)

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
        files = await _files(conn, invoice_id)
    out_root = tmp_path / "tmp" / str(invoice_id)
    assert not out_root.exists() or not any(out_root.rglob("*"))
    assert attempts == []
    assert files == []


async def test_commit_that_raises_after_the_fetch_removes_its_folder(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-9.2: a fetch whose commit raises (here StateMoved) rolls back and leaves no files."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    handler = _handler(db_pool, FakeFetcher(FetchReason.OK), tmp_path)

    async def moved(conn: AsyncConnection[Any], invoice_id: int, state: str, **kwargs: Any) -> None:
        raise StateMoved(invoice_id, kwargs.get("expected"), "DELIVERING")

    monkeypatch.setattr("invoicebot.pipeline.fetch.set_state", moved)
    with pytest.raises(StateMoved):
        await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
        files = await _files(conn, invoice_id)
    out_root = tmp_path / "tmp" / str(invoice_id)
    assert not out_root.exists() or not any(out_root.rglob("*"))
    assert attempts == []
    assert files == []


async def test_cancel_after_the_commit_keeps_the_committed_files(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1: a cancellation after the commit keeps the files the committed rows point to."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    pool = _CancellingPool(db_pool, cancel_on=2)
    handler = _handler(pool, FakeFetcher(FetchReason.OK), tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
    assert invoice == ("DELIVERING", None, FLOW)
    assert [kind for kind, _path, _sha, _size in files] == ["pdf", "xml"]
    assert _all_exist([path for _kind, path, _sha, _size in files])


async def test_invoice_cancelled_mid_fetch_stays_cancelled_and_writes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D95: an invoice cancelled during its fetch keeps CANCELLED with no files or message."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = CancellingFetcher(db_pool, invoice_id)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("CANCELLED", "BR-S07", FLOW)
    assert files == []
    assert attempts == []
    assert deliver_jobs == []
    assert outbox == []


async def test_two_runs_of_one_job_use_different_out_dirs(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.2: each run of a job gets its own folder, so one cannot remove the other's files."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = RecordingFetcher()
    handler = _handler(db_pool, fetcher, tmp_path)
    job = _job(invoice_id)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic fetch failure"):
            await handler(job)

    assert len(fetcher.out_dirs) == 2
    assert fetcher.out_dirs[0] != fetcher.out_dirs[1]


async def test_not_found_yet_past_the_last_retry_ends_manual_required(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F07: a fetcher whose retries are used up sends the invoice to MANUAL_REQUIRED."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "not_found_yet", 3)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert len(attempts) == 3
    assert fetcher.calls == 0
    assert len(outbox) == 1
    text, rule_id = outbox[0]
    assert rule_id == "BR-F07"
    assert text.startswith(f"❗ No pude obtener el XML/PDF de {SERIE}-{NUMERO}.")
    assert "• Portal del comercio: no encontré el comprobante" in text
    assert "Puedes enviarme el PDF y el XML aquí" in text


async def test_chain_end_manual_required_carries_retry_and_cancel_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D190, BR-F07: the chain's last message carries [🔄 Reintentar] and [✖️ Anular] for its invoice."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "not_found_yet", 3)

    await _run(db_pool, FakeFetcher(FetchReason.NOT_FOUND_YET), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        buttons = await _outbox_button_dicts(conn, profile_id)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07"]
    assert buttons == [
        [
            {"id": f"/reprocesar {invoice_id}", "title": "🔄 Reintentar"},
            {"id": f"/cancelar {invoice_id}", "title": "✖️ Anular"},
        ]
    ]


async def test_xml_mismatch_ends_manual_required_with_its_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F07: a final xml_mismatch moves on to the next run, which ends in MANUAL_REQUIRED."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.XML_MISMATCH)

    await _run(db_pool, fetcher, invoice_id, tmp_path)
    async with db_pool.connection() as conn:
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)

    await _run(db_pool, fetcher, invoice_id, tmp_path, next_job_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert fetcher.calls == 1
    assert attempts == [("portal_registry", PORTAL_KEY, False, "xml_mismatch")]
    assert len(outbox) == 1
    assert "• Portal del comercio: el XML no coincide" in outbox[0][0]


async def test_no_portal_exhaustion_raises_one_linked_gap_and_the_gap_line(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F02: an exhausted chain with no_portal makes one linked gap and ends BR-F07 with its line."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "no_portal", 1)

    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        gaps = await _gaps(conn)
        links = await _gap_links(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert gaps == [(f"portal_missing:{STORE_RUC}", "PORTAL_MISSING")]
    assert links == [f"portal_missing:{STORE_RUC}"]
    # BR-F02 asks this sender for the portal after BR-F07's message.
    assert len(outbox) == 2
    text, rule_id = outbox[0]
    assert rule_id == "BR-F07"
    assert text.endswith(GAP_LINE_PORTAL)


async def test_not_found_exhaustion_raises_no_gap_and_leaves_no_trailing_space(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-11.3: an exhausted chain with only not_found raises no gap and ends BR-F07 at "lo completo."."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "not_found", 1)

    await _run(db_pool, FakeFetcher(FetchReason.NOT_FOUND), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        gaps = await _gaps(conn)
        links = await _gap_links(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert gaps == []
    assert links == []
    assert len(outbox) == 1
    text, rule_id = outbox[0]
    assert rule_id == "BR-F07"
    assert text.endswith("lo completo.")


async def test_two_invoices_of_one_issuer_share_one_gap_with_two_links(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-11.2: two exhausted invoices of one issuer make one gap linked to both."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_ids = [await _fetching_invoice(db_pool, profile_id) for _ in range(2)]

    for invoice_id in invoice_ids:
        await _seed_attempts(db_pool, invoice_id, "no_portal", 1)
        await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        gaps = await _gaps(conn)
        links = [await _gap_links(conn, invoice_id) for invoice_id in invoice_ids]
        cur = await conn.execute(
            "SELECT count FROM gaps WHERE fingerprint = %s",
            (f"portal_missing:{STORE_RUC}",),
        )
        row = await cur.fetchone()
    assert gaps == [(f"portal_missing:{STORE_RUC}", "PORTAL_MISSING")]
    assert links == [[f"portal_missing:{STORE_RUC}"], [f"portal_missing:{STORE_RUC}"]]
    assert row == (2,)


async def test_invoice_outside_fetching_docs_is_left_alone(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-5.1: an invoice in ROUTING is not fetched and nothing about it changes."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id, state="ROUTING")
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        files = await _files(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("ROUTING", None, FLOW)
    assert fetcher.calls == 0
    assert files == []
    assert attempts == []
    assert jobs == []
    assert outbox == []


async def test_flow_with_nothing_to_fetch_moves_to_delivering(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D144: a flow with no fetchers and no required documents moves to DELIVERING with one deliver job."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path, cfg=_config((), required_docs=()))

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        files = await _files(conn, invoice_id)
        fetch_jobs = await _fetch_jobs(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert invoice == ("DELIVERING", None, FLOW)
    assert fetcher.calls == 0
    assert attempts == []
    assert files == []
    assert fetch_jobs == []
    assert len(deliver_jobs) == 1


async def test_flow_with_nothing_to_fetch_but_a_required_document_ends_manual_required(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D144: a flow with no fetchers but a required document still ends in MANUAL_REQUIRED with BR-F07."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path, cfg=_config((), required_docs=("pdf",)))

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert fetcher.calls == 0
    assert attempts == []
    assert deliver_jobs == []
    assert len(outbox) == 1
    text, rule_id = outbox[0]
    assert rule_id == "BR-F07"
    assert text.startswith(f"❗ No pude obtener el XML/PDF de {SERIE}-{NUMERO}.")


async def test_superseded_nothing_to_fetch_job_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D144: a superseded job on a nothing-to-fetch flow moves no invoice and enqueues nothing."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn, conn.transaction():
        job_id = await enqueue(conn, invoice_id, FETCH_STEP)
        await enqueue(conn, invoice_id, FETCH_STEP)
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path, job_id=job_id, cfg=_config((), required_docs=()))

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        fetch_jobs = await _fetch_jobs(conn, invoice_id)
        deliver_jobs = await _deliver_jobs(conn, invoice_id)
    assert invoice == ("FETCHING_DOCS", None, FLOW)
    assert fetcher.calls == 0
    assert attempts == []
    assert len(fetch_jobs) == 2  # the two queued jobs are untouched
    assert deliver_jobs == []


async def test_flow_registry_missing_a_listed_fetcher_skips_it(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S7.1, S11.9, D100: a fetcher the flow lists but its registry lacks is skipped."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = make_fetch_handler(
        db_pool,
        _config(("email_inbox", "portal_registry")),
        _directory(),
        {FLOW: {"portal_registry": fetcher}},
        tmp_path,
    )

    await handler(_job(invoice_id))

    async with db_pool.connection() as conn:
        attempts = await _attempts(conn, invoice_id)
    assert fetcher.calls == 1
    assert attempts == [("portal_registry", PORTAL_KEY, False, "not_found_yet")]


async def test_inbox_second_not_found_yet_retries_in_1h(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S7.1, S11.9, D100: the inbox's second wait uses its flow schedule's 1 h delay."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = make_fetch_handler(
        db_pool,
        _config(("email_inbox",)),
        _directory(),
        {FLOW: {"email_inbox": fetcher}},
        tmp_path,
        schedules={EMAIL_INBOX_FETCHER: _inbox_schedule},
    )
    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)

    await handler(_job(invoice_id, retry_job_id))

    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT max(run_after) - now() FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, FETCH_STEP),
        )
        row = await cur.fetchone()
    assert fetcher.calls == 2
    assert row is not None
    assert MINUTES_55 <= row[0].total_seconds() / 60 <= MINUTES_65


async def test_first_wait_sends_each_fetchers_text_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S7.1, S11.9, D100 (D189): each fetcher's first wait sends its own text, once."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    portal = FakeFetcher(FetchReason.NOT_FOUND_YET)
    inbox = FakeFetcher(FetchReason.NOT_FOUND_YET)
    directory = _directory()
    portal_cfg = directory.portals[PORTAL_KEY].model_copy(update={"retry_schedule": ["1h"]})
    directory = directory.model_copy(update={"portals": {PORTAL_KEY: portal_cfg}})
    handler = make_fetch_handler(
        db_pool,
        _config(("portal_registry", "email_inbox")),
        directory,
        {FLOW: {"portal_registry": portal, "email_inbox": inbox}},
        tmp_path,
        schedules={EMAIL_INBOX_FETCHER: _inbox_schedule, PORTAL_FETCHER: _portal_schedule(directory)},
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        second_job_id = await _latest_fetch_job_id(conn, invoice_id)
    # The portal's one retry is spent; its second wait has no delay and says nothing.
    await handler(_job(invoice_id, second_job_id))
    async with db_pool.connection() as conn:
        third_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, third_job_id))

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    texts = [text for text, _rule_id in outbox]
    rule_ids = [rule_id for _text, rule_id in outbox]
    assert texts.count(FETCH_WAITING.format(id=invoice_id)) == 1
    assert rule_ids.count(RULE_DOCS_NEEDED) == 1


async def _run_portal_then_inbox(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, tmp_path: Path
) -> tuple[Config, FakeFetcher, FakeFetcher]:
    """Run an unknown issuer's portal (no portal) and then its first inbox wait."""
    portal = FakeFetcher(FetchReason.NO_PORTAL)
    inbox = FakeFetcher(FetchReason.NOT_FOUND_YET)
    directory = _directory()
    cfg = _config(("portal_registry", "email_inbox"))
    handler = make_fetch_handler(
        db_pool,
        cfg,
        directory,
        {FLOW: {"portal_registry": portal, "email_inbox": inbox}},
        tmp_path,
        schedules={EMAIL_INBOX_FETCHER: _inbox_schedule, PORTAL_FETCHER: _portal_schedule(directory)},
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        second_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, second_job_id))
    return cfg, portal, inbox


async def test_inbox_first_wait_offers_the_docs_needed_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D189, D190 (BR-F13): an unknown issuer's first inbox wait offers [🔗] [📎] and the no-portal bullet."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading=_reading_for(UNKNOWN_RUC))

    cfg, _portal, _inbox = await _run_portal_then_inbox(db_pool, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        buttons = await _outbox_buttons(conn, profile_id)
        cur = await conn.execute(
            "SELECT max(run_after), (now() AT TIME ZONE %s)::date FROM jobs"
            " WHERE invoice_id = %s AND step = %s",
            (cfg.owner.timezone, invoice_id, FETCH_STEP),
        )
        row = await cur.fetchone()
    assert row is not None
    next_at, today = row
    # The schedule's 5m first look, then the 1h, 6h and 24h waits after it.
    wait_start = next_at - timedelta(minutes=5)
    last_at = wait_start + timedelta(hours=31, minutes=5)
    last_desc = when_desc(last_at.astimezone(ZoneInfo(cfg.owner.timezone)), today)
    assert [rule_id for _text, rule_id in outbox] == [RULE_DOCS_NEEDED]
    text = outbox[0][0]
    assert FETCH_NO_PORTAL_LINE in text
    assert f"Mientras tanto, reviso tu Outlook hasta {last_desc} por si el comercio te lo envió" in text
    assert buttons == [
        [
            f"{invoice_id}:{TEACH_PORTAL_ACTION}",
            f"{invoice_id}:{SEND_PDF_ACTION}",
        ]
    ]


async def test_inbox_first_wait_names_the_last_look_after_the_whole_schedule(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D190, BR-F13: the first inbox wait names its last look, 31 h 5 m after the wait started."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading=_reading_for(UNKNOWN_RUC))

    cfg, _portal, _inbox = await _run_portal_then_inbox(db_pool, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        cur = await conn.execute(
            "SELECT max(run_after), (now() AT TIME ZONE %s)::date FROM jobs"
            " WHERE invoice_id = %s AND step = %s",
            (cfg.owner.timezone, invoice_id, FETCH_STEP),
        )
        row = await cur.fetchone()
    assert row is not None
    next_at, today = row
    # The schedule's 5m first look, then the 1h, 6h and 24h waits after it.
    wait_start = next_at - timedelta(minutes=5)
    last_at = wait_start + timedelta(hours=31, minutes=5)
    last_desc = when_desc(last_at.astimezone(ZoneInfo(cfg.owner.timezone)), today)
    assert (
        f"Mientras tanto, reviso tu Outlook hasta {last_desc} por si el comercio te lo envió" in outbox[0][0]
    )


async def test_inbox_first_wait_hides_the_portal_button_with_a_taught_link(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D189, D190 (BR-F13): an issuer with a taught link gets [📎] only."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading=_reading_for(UNKNOWN_RUC))
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO taught_links (ruc_emisor, url, profile_id, state)
            VALUES (%s, %s, %s, 'proposed')
            """,
            (int(UNKNOWN_RUC), "https://portal.example.pe/consulta", profile_id),
        )

    await _run_portal_then_inbox(db_pool, invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        buttons = await _outbox_buttons(conn, profile_id)
    assert buttons == [[f"{invoice_id}:{SEND_PDF_ACTION}"]]


async def test_inbox_second_wait_sends_no_new_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D189 (BR-F13): only the inbox's first wait queues the docs-needed message."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading=_reading_for(UNKNOWN_RUC))
    inbox = FakeFetcher(FetchReason.NOT_FOUND_YET)
    handler = make_fetch_handler(
        db_pool,
        _config(("email_inbox",)),
        _directory(),
        {FLOW: {"email_inbox": inbox}},
        tmp_path,
        schedules={EMAIL_INBOX_FETCHER: _inbox_schedule},
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, retry_job_id))

    async with db_pool.connection() as conn:
        rule_ids = [rule_id for _text, rule_id in await _outbox(conn, profile_id)]
    assert inbox.calls == 2
    assert rule_ids == [RULE_DOCS_NEEDED]


async def test_portal_first_wait_says_waiting_without_buttons(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D100 (D189): a portal fetcher's first wait keeps FETCH_WAITING and carries no buttons."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)

    await _run(db_pool, FakeFetcher(FetchReason.NOT_FOUND_YET), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        buttons = await _outbox_buttons(conn, profile_id)
    assert outbox == [(FETCH_WAITING.format(id=invoice_id), None)]
    assert buttons == [[]]


async def test_inbox_auth_required_alerts_admins_once_and_retries(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S7.1, S11.9, D100: an inbox auth_required alerts each admin once and schedules the next try."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.AUTH_REQUIRED)
    cfg = _config(("email_inbox",))
    handler = make_fetch_handler(db_pool, cfg, _directory(), {FLOW: {"email_inbox": fetcher}}, tmp_path)

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, retry_job_id))

    async with db_pool.connection() as conn:
        alerts = {
            admin_id: [text for text, rule_id in await _outbox(conn, admin_id) if rule_id == "BR-D01_admin"]
            for admin_id, profile in cfg.profiles.items()
            if profile.admin
        }
        cur = await conn.execute(
            "SELECT max(run_after) - now() FROM jobs WHERE invoice_id = %s AND step = %s",
            (invoice_id, FETCH_STEP),
        )
        row = await cur.fetchone()
    assert {admin_id: len(texts) for admin_id, texts in alerts.items()} == {"yo": 1}
    assert "--account bot" in alerts["yo"][0]
    assert row is not None
    assert row[0].total_seconds() > 0


async def test_gmail_inbox_auth_required_alerts_with_google_slug(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-D01: a Gmail inbox auth_required names auth_google and the account in the admin text."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.AUTH_REQUIRED)
    cfg = _config(("email_inbox",))
    flow = cfg.flows[FLOW]
    inbox = flow.email_inbox
    assert inbox is not None
    gmail = inbox.model_copy(update={"provider": "gmail", "account": "invoices_a"})
    flow = flow.model_copy(update={"email_inbox": gmail})
    cfg = cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})
    handler = make_fetch_handler(db_pool, cfg, _directory(), {FLOW: {"email_inbox": fetcher}}, tmp_path)

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, retry_job_id))

    async with db_pool.connection() as conn:
        alerts = {
            admin_id: [
                text
                for text, rule_id in await _outbox(conn, admin_id)
                if rule_id == "BR-D01_admin" and "auth_google" in text
            ]
            for admin_id, profile in cfg.profiles.items()
            if profile.admin
        }
    assert {admin_id: len(texts) for admin_id, texts in alerts.items()} == {"yo": 1}
    assert "invoices_a" in alerts["yo"][0]


async def test_reading_document_url_reaches_the_fetcher(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """S7.1, S11.9, D100: the reading's document_url reaches the fetcher's FetchInvoice."""
    profile_id = f"fetch-{uuid4().hex}"
    reading = Reading(fields=_reading().fields, document_url="https://example.test/comprobante")
    invoice_id = await _fetching_invoice(db_pool, profile_id, reading=reading)
    fetcher = FakeFetcher(FetchReason.OK)

    await _run(db_pool, fetcher, invoice_id, tmp_path)

    assert fetcher.invoices[0].document_url == "https://example.test/comprobante"


async def test_teach_first_no_portal_exhaustion_queues_f07_then_f02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D191, BR-F02: an unknown issuer's first exhausted invoice gets BR-F07, then BR-F02 and its buttons."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "no_portal", 1)

    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
        buttons = await _outbox_button_dicts(conn, profile_id)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07", "BR-F02"]
    ask_text, _rule_id = outbox[1]
    assert ask_text == MESSAGES["BR-F02"].format(razon_social="este emisor", ruc_emisor=STORE_RUC)
    assert buttons[1] == [
        {"id": f"u{invoice_id}:fix", "title": BUTTON_WRITE_PORTAL},
        {"id": f"u{invoice_id}:dk", "title": BUTTON_DONT_KNOW},
    ]


async def test_teach_second_no_portal_exhaustion_of_the_issuer_queues_f07_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F02: a second exhausted invoice of the same issuer gets BR-F07 only."""
    first_profile = f"fetch-{uuid4().hex}"
    second_profile = f"fetch-{uuid4().hex}"
    first_id = await _fetching_invoice(db_pool, first_profile)
    second_id = await _fetching_invoice(db_pool, second_profile)
    for invoice_id in (first_id, second_id):
        await _seed_attempts(db_pool, invoice_id, "no_portal", 1)

    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), first_id, tmp_path)
    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), second_id, tmp_path)

    async with db_pool.connection() as conn:
        first_outbox = await _outbox(conn, first_profile)
        second_outbox = await _outbox(conn, second_profile)
    assert [rule_id for _text, rule_id in first_outbox] == ["BR-F07", "BR-F02"]
    assert [rule_id for _text, rule_id in second_outbox] == ["BR-F07"]


async def test_teach_via_chat_off_queues_f07_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F02: with gaps.teach_via_chat false the exhausted invoice gets BR-F07 only."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "no_portal", 1)
    cfg = _config()
    off = cfg.model_copy(update={"gaps": cfg.gaps.model_copy(update={"teach_via_chat": False})})

    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), invoice_id, tmp_path, cfg=off)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07"]


async def test_teach_existing_taught_link_queues_f07_only(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-F02: an issuer whose taught link is already stored gets BR-F07 only."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    await _seed_attempts(db_pool, invoice_id, "no_portal", 1)
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO taught_links (ruc_emisor, url, profile_id, state)
            VALUES (%s, %s, %s, 'proposed')
            """,
            (int(STORE_RUC), "https://portal.example.pe/consulta", profile_id),
        )

    await _run(db_pool, FakeFetcher(FetchReason.NO_PORTAL), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07"]


@pytest_asyncio.fixture(loop_scope="session")
async def _clean_fetch_attempts(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start and end a BR-F03 test with no fetch attempts, so its portal streak is its own."""

    async def clear() -> None:
        async with db_pool.connection() as conn:
            await conn.execute("DELETE FROM fetch_attempts")

    await clear()
    yield
    await clear()


@pytest.mark.parametrize("reason", [FetchReason.TECHNICAL, FetchReason.TIMEOUT], ids=["technical", "timeout"])
@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_first_technical_failure_schedules_one_retry_without_a_message(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path, reason: FetchReason
) -> None:
    """D167: a first technical or timeout failure records one attempt and retries in about 1 h, silently."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)

    await _run(db_pool, FakeFetcher(reason), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        attempts = await _attempts(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
        delay = await _latest_delay(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("FETCHING_DOCS", None, FLOW)
    assert attempts == [("portal_registry", PORTAL_KEY, False, reason.value)]
    assert len(jobs) == 1
    assert MINUTES_55 <= delay.total_seconds() / 60 <= MINUTES_65
    assert outbox == []


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_second_technical_moves_the_chain_on_at_once(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D167: a second technical failure spends the retry, and the next fetcher runs at once."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    first = FakeFetcher(FetchReason.TECHNICAL)
    second = FakeFetcher(FetchReason.OK)
    handler = make_fetch_handler(
        db_pool,
        _config(("portal_registry", "manual")),
        _directory(),
        {FLOW: {"portal_registry": first, "manual": second}},
        tmp_path,
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await handler(_job(invoice_id, retry_job_id))
    async with db_pool.connection() as conn:
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
    assert abs((jobs[-1] - datetime.now(UTC)).total_seconds()) < 10

    await handler(_job(invoice_id, next_job_id))

    assert (first.calls, second.calls) == (2, 1)


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_second_technical_with_one_fetcher_ends_manual_required(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D167: a second technical failure with no next fetcher ends in MANUAL_REQUIRED with BR-F07."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    fetcher = FakeFetcher(FetchReason.TECHNICAL)

    await _run(db_pool, fetcher, invoice_id, tmp_path)
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await _run(db_pool, fetcher, invoice_id, tmp_path, retry_job_id)
    async with db_pool.connection() as conn:
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await _run(db_pool, fetcher, invoice_id, tmp_path, next_job_id)

    async with db_pool.connection() as conn:
        invoice = await _invoice(conn, invoice_id)
        outbox = await _outbox(conn, profile_id)
    assert invoice == ("MANUAL_REQUIRED", "BR-F07", FLOW)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07"]
    assert fetcher.calls == 2


async def _technical_failures(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    profile_id: str,
    count: int,
) -> list[int]:
    """Create ``count`` invoices of ``profile_id`` and run one technical fetch for each."""
    invoice_ids = []
    for _ in range(count):
        invoice_id = await _fetching_invoice(db_pool, profile_id)
        await _run(db_pool, FakeFetcher(FetchReason.TECHNICAL), invoice_id, tmp_path)
        invoice_ids.append(invoice_id)
    return invoice_ids


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_broken_three_technical_failures_raise_one_portal_broken_gap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-F03: three technical failures in a row on one portal raise one PORTAL_BROKEN gap."""
    profile_id = f"fetch-{uuid4().hex}"

    invoice_ids = await _technical_failures(db_pool, tmp_path, profile_id, 3)

    async with db_pool.connection() as conn:
        gaps = await _gaps(conn)
        links = await _gap_links(conn, invoice_ids[-1])
    assert gaps == [(f"portal_broken:{PORTAL_KEY}", "PORTAL_BROKEN")]
    assert links == [f"portal_broken:{PORTAL_KEY}"]


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_broken_sequence_with_a_not_found_raises_no_gap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-F03: technical, not_found, technical, technical with broken_after 3 raises no gap."""
    profile_id = f"fetch-{uuid4().hex}"
    reasons = (
        FetchReason.TECHNICAL,
        FetchReason.NOT_FOUND,
        FetchReason.TECHNICAL,
        FetchReason.TECHNICAL,
    )

    for reason in reasons:
        invoice_id = await _fetching_invoice(db_pool, profile_id)
        await _run(db_pool, FakeFetcher(reason), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        gaps = await _gaps(conn)
    assert gaps == []


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_broken_third_invoices_br_f07_names_the_broken_portal(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
) -> None:
    """BR-F03: the third invoice's BR-F07 bullet is BR-F03's line, not the technical one."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_id = (await _technical_failures(db_pool, tmp_path, profile_id, 3))[-1]
    async with db_pool.connection() as conn:
        retry_job_id = await _latest_fetch_job_id(conn, invoice_id)

    # D167: the invoice's first technical failure waits, so its retry spends the one retry
    # and only the job after it ends the chain in BR-F07.
    await _run(db_pool, FakeFetcher(FetchReason.TECHNICAL), invoice_id, tmp_path, retry_job_id)
    async with db_pool.connection() as conn:
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)
    await _run(db_pool, FakeFetcher(FetchReason.TECHNICAL), invoice_id, tmp_path, next_job_id)

    async with db_pool.connection() as conn:
        outbox = await _outbox(conn, profile_id)
    assert [rule_id for _text, rule_id in outbox] == ["BR-F07"]
    text = outbox[0][0]
    assert MESSAGES["BR-F03"].format(portal=PORTAL_KEY) in text
    assert FETCH_TECHNICAL_LINE.format(fetcher=fetcher_desc(PORTAL_FETCHER)) not in text


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_open_broken_gap_ends_the_portals_retry(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D170: with an open PORTAL_BROKEN gap, a technical portal failure retries at once and moves on."""
    profile_id = f"fetch-{uuid4().hex}"
    await _technical_failures(db_pool, tmp_path, profile_id, 3)
    invoice_id = await _fetching_invoice(db_pool, profile_id)
    first = FakeFetcher(FetchReason.TECHNICAL)
    second = FakeFetcher(FetchReason.OK)
    handler = make_fetch_handler(
        db_pool,
        _config(("portal_registry", "manual")),
        _directory(),
        {FLOW: {"portal_registry": first, "manual": second}},
        tmp_path,
    )

    await handler(_job(invoice_id))
    async with db_pool.connection() as conn:
        next_job_id = await _latest_fetch_job_id(conn, invoice_id)
        jobs = await _fetch_jobs(conn, invoice_id)
    assert len(jobs) == 1
    assert abs((jobs[-1] - datetime.now(UTC)).total_seconds()) < 10

    await handler(_job(invoice_id, next_job_id))

    assert (first.calls, second.calls) == (1, 1)


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_the_failure_that_raises_the_gap_gets_no_retry(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D170: the technical failure that raises PORTAL_BROKEN enqueues its next job with no delay."""
    profile_id = f"fetch-{uuid4().hex}"
    invoice_ids = await _technical_failures(db_pool, tmp_path, profile_id, 3)
    invoice_id = invoice_ids[-1]

    async with db_pool.connection() as conn:
        gaps = await _gaps(conn)
        jobs = await _fetch_jobs(conn, invoice_id)
    assert gaps == [(f"portal_broken:{PORTAL_KEY}", "PORTAL_BROKEN")]
    assert len(jobs) == 1
    assert abs((jobs[-1] - datetime.now(UTC)).total_seconds()) < 10


@pytest.mark.usefixtures("_clean_fetch_attempts")
async def test_open_broken_gap_ends_the_retry_without_a_streak(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D170: an open gap alone ends the retry, even when this failure trips no BR-F03 streak."""
    profile_id = f"fetch-{uuid4().hex}"
    await _technical_failures(db_pool, tmp_path, profile_id, 3)
    # A later invoice that ended not_found_yet breaks the streak; the gap stays open.
    quiet_id = await _fetching_invoice(db_pool, profile_id)
    async with db_pool.connection() as conn:
        await record_attempt(
            conn, quiet_id, "portal_registry", PORTAL_KEY, FetchReason.NOT_FOUND_YET.value, 1
        )
    invoice_id = await _fetching_invoice(db_pool, profile_id)

    await _run(db_pool, FakeFetcher(FetchReason.TECHNICAL), invoice_id, tmp_path)

    async with db_pool.connection() as conn:
        gaps = await _gaps(conn)
        jobs = await _fetch_jobs(conn, invoice_id)
    assert gaps == [(f"portal_broken:{PORTAL_KEY}", "PORTAL_BROKEN")]
    assert len(jobs) == 1
    assert abs((jobs[-1] - datetime.now(UTC)).total_seconds()) < 10
