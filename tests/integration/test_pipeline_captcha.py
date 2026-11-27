"""Integration tests for the fetch step's captcha ask and resume (D209, BR-F18, BR-F20)."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.config.loader import load_config
from invoicebot.config.models import Brand, Config, Portal, Store, StoreDirectory
from invoicebot.extract.fields import Field, Reading
from invoicebot.fetchers.base import FetchedFile, Fetcher, FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.protocol import CaptchaChallenge, CaptchaForm
from invoicebot.pipeline.fetch import FETCH_STEP, PORTAL_FETCHER, make_fetch_handler
from invoicebot.pipeline.steps import CAPTCHA_TIMEOUT_STEP, DELIVER_STEP
from invoicebot.queue.jobs import Job
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.store import captchas
from invoicebot.store.fetch_attempts import record_attempt
from invoicebot.store.invoices import create_invoice, save_reading, set_flow, set_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"
# From tests/synthetic_rucs.txt; no real RUC appears here.
STORE_RUC = "20000000001"
COMERCIO = "Tienda Sintética"
PORTAL_KEY = "synthetic_portal"
PORTAL_URL = "https://portal.ejemplo.pe/consulta"
FLOW = "work_microsoft"
NEXT_FETCHER = "qr_url"
ANSWER = "ab12"
PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
PNG_BYTES = b"\x89PNG synthetic captcha"
#: The example config's ask_user_timeout_s of 150 s, in whole minutes rounded down (BR-F18).
TIMEOUT_MINUTES = 2


def _config() -> Config:
    """Return the example config whose work flow tries the portal, then the QR link."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows[FLOW].model_copy(
        update={"fetchers": [PORTAL_FETCHER, NEXT_FETCHER], "required_docs": ["pdf"]}
    )
    return cfg.model_copy(update={"flows": {**cfg.flows, FLOW: flow}})


def _directory() -> StoreDirectory:
    """Return a store directory with one store whose active portal asks a captcha."""
    portal = Portal(key=PORTAL_KEY, adapter="generic_form", status="active", url=PORTAL_URL)
    store = Store(
        ruc=STORE_RUC,
        razon_social="Comercio Sintético",
        tipo="01",
        brands=[Brand(comercio=COMERCIO)],
        portal=PORTAL_KEY,
    )
    return StoreDirectory(stores={STORE_RUC: store}, portals={PORTAL_KEY: portal})


def _field(value: str) -> Field:
    return Field(value=value, source="qr", confidence=1.0)


def _reading() -> Reading:
    """Build a complete synthetic reading the fetchers need."""
    return Reading(
        fields={
            "ruc_emisor": _field(STORE_RUC),
            "tipo": _field("01"),
            "serie": _field("F001"),
            "numero": _field("15160"),
            "total": _field("49.90"),
            "fecha_emision": _field("2026-11-13"),
        },
        document_url=None,
    )


def _form() -> CaptchaForm:
    """Return a synthetic captcha form on the portal's own host."""
    return CaptchaForm(
        page_url=PORTAL_URL,
        action_url=f"{PORTAL_URL}/enviar",
        fields=(("serie", "F001"), ("numero", "15160")),
        captcha_name="captcha",
        submit_name="ok",
        submit_value="Consultar",
    )


def _pdf(out_dir: Path) -> FetchResult:
    """Write the invoice PDF into ``out_dir`` and return the ok result that names it."""
    path = out_dir / "invoice.pdf"
    path.write_bytes(PDF_BYTES)
    file = FetchedFile(
        kind="pdf", path=path, size=len(PDF_BYTES), sha256=hashlib.sha256(PDF_BYTES).hexdigest()
    )
    return FetchResult(FetchReason.OK, (file,), PORTAL_KEY)


def _asked(out_dir: Path) -> FetchResult:
    """Write a captcha PNG into ``out_dir`` and return the ``captcha_asked`` result that names it."""
    path = out_dir / "captcha.png"
    path.write_bytes(PNG_BYTES)
    challenge = CaptchaChallenge(
        path=path, size=len(PNG_BYTES), sha256=hashlib.sha256(PNG_BYTES).hexdigest(), form=_form()
    )
    return FetchResult(FetchReason.CAPTCHA_ASKED, (), PORTAL_KEY, challenge)


class FakePortal:
    """A portal fetcher: ``fetch`` and ``resume`` each return the next scripted reason."""

    def __init__(self, fetch: FetchReason, resume: FetchReason = FetchReason.OK) -> None:
        self._fetch = fetch
        self._resume = resume
        self.fetches = 0
        self.resumes: list[tuple[str, CaptchaForm]] = []

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        del invoice
        self.fetches += 1
        return self._result(self._fetch, out_dir)

    async def resume(
        self, invoice: FetchInvoice, form: CaptchaForm, answer: str, out_dir: Path
    ) -> FetchResult:
        del invoice
        self.resumes.append((answer, form))
        return self._result(self._resume, out_dir)

    @staticmethod
    def _result(reason: FetchReason, out_dir: Path) -> FetchResult:
        if reason is FetchReason.OK:
            return _pdf(out_dir)
        if reason is FetchReason.CAPTCHA_ASKED:
            return _asked(out_dir)
        return FetchResult(reason, (), PORTAL_KEY)


class FakeNext:
    """The chain's next fetcher: it always finds the PDF."""

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, invoice: FetchInvoice, out_dir: Path) -> FetchResult:
        del invoice
        self.calls += 1
        return _pdf(out_dir)


async def _invoice(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> int:
    """Create a routed, fetching invoice with a stored reading and return its id."""
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, f"captcha-{uuid4().hex}", "whatsapp")
        await save_reading(conn, invoice_id, _reading())
        await set_flow(conn, invoice_id, FLOW)
        await set_state(conn, invoice_id, "FETCHING_DOCS")
    return invoice_id


async def _answered_row(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, data_dir: Path
) -> Path:
    """Seed one asked-and-answered captcha with its PNG on disk and return the PNG's path."""
    image = data_dir / "captchas" / str(invoice_id) / "seed.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(PNG_BYTES)
    async with pool.connection() as conn, conn.transaction():
        await captchas.put(conn, invoice_id, PORTAL_KEY, _form().model_dump(mode="json"), str(image), 300)
        assert await captchas.set_answer(conn, invoice_id, ANSWER)
    return image


async def _attempt_reasons(
    pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, *reasons: FetchReason
) -> None:
    """Record ``reasons`` as the portal's earlier attempts."""
    async with pool.connection() as conn, conn.transaction():
        for reason in reasons:
            await record_attempt(conn, invoice_id, PORTAL_FETCHER, PORTAL_KEY, reason.value, 1)


async def _run(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    registry: dict[str, Fetcher],
    invoice_id: int,
    data_dir: Path,
    job_id: int = 1,
) -> None:
    """Run the fetch handler once for ``invoice_id``."""
    handler = make_fetch_handler(pool, _config(), _directory(), {FLOW: registry}, data_dir)
    job = Job(
        id=job_id,
        invoice_id=invoice_id,
        step=FETCH_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )
    await handler(job)


async def _state(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> tuple[str, str | None]:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return str(row[0]), row[1]


async def _outbox(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT payload_json FROM outbox WHERE invoice_id = %s ORDER BY id", (invoice_id,)
        )
        return [row[0] for row in await cur.fetchall()]


async def _count(pool: AsyncConnectionPool[AsyncConnection[Any]], sql: str, invoice_id: int) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, (invoice_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _jobs(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int, step: str) -> list[int]:
    """Return the ids of the invoice's queued jobs of ``step``, oldest first."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM jobs WHERE invoice_id = %s AND step = %s ORDER BY id", (invoice_id, step)
        )
        return [int(row[0]) for row in await cur.fetchall()]


async def _attempt_log(pool: AsyncConnectionPool[AsyncConnection[Any]], invoice_id: int) -> list[str]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT reason FROM fetch_attempts WHERE invoice_id = %s ORDER BY id", (invoice_id,)
        )
        return [str(row[0]) for row in await cur.fetchall()]


_ROWS = "SELECT count(*) FROM portal_captchas WHERE invoice_id = %s"


async def test_captcha_asked_parks_the_invoice_and_sends_the_image(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209, BR-F18: captcha_asked waits in WAITING_CAPTCHA: one document, one row, one timeout job."""
    invoice_id = await _invoice(db_pool)

    await _run(db_pool, {PORTAL_FETCHER: FakePortal(FetchReason.CAPTCHA_ASKED)}, invoice_id, tmp_path)

    assert await _state(db_pool, invoice_id) == ("WAITING_CAPTCHA", "BR-F18")
    assert await _attempt_log(db_pool, invoice_id) == ["captcha_asked"]
    messages = await _outbox(db_pool, invoice_id)
    assert len(messages) == 1
    assert messages[0]["rule_id"] == "BR-F18"
    assert messages[0]["text"] == MESSAGES["BR-F18"].format(portal=COMERCIO, minutes=TIMEOUT_MINUTES)
    document = messages[0]["document"]
    assert await asyncio.to_thread(Path(document["path"]).read_bytes) == PNG_BYTES
    assert Path(document["path"]).is_relative_to(tmp_path / "captchas" / str(invoice_id))
    assert document["mime"] == "image/png"
    assert await _count(db_pool, _ROWS, invoice_id) == 1
    assert await _jobs(db_pool, invoice_id, FETCH_STEP) == []
    assert len(await _jobs(db_pool, invoice_id, CAPTCHA_TIMEOUT_STEP)) == 1
    async with db_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT j.run_after = c.expires_at FROM jobs j JOIN portal_captchas c USING (invoice_id)"
            " WHERE j.invoice_id = %s AND j.step = %s",
            (invoice_id, CAPTCHA_TIMEOUT_STEP),
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is True
    assert not (tmp_path / "tmp" / str(invoice_id)).exists() or not any(
        (tmp_path / "tmp" / str(invoice_id)).rglob("*.png")
    )


async def test_answered_captcha_resumes_the_portal_and_clears_the_row(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: an answered row resumes the portal with the answer; ok delivers and the row and PNG go."""
    invoice_id = await _invoice(db_pool)
    await _attempt_reasons(db_pool, invoice_id, FetchReason.CAPTCHA_ASKED)
    image = await _answered_row(db_pool, invoice_id, tmp_path)
    portal = FakePortal(FetchReason.CAPTCHA_ASKED, resume=FetchReason.OK)

    await _run(db_pool, {PORTAL_FETCHER: portal}, invoice_id, tmp_path)

    assert [answer for answer, _form_used in portal.resumes] == [ANSWER]
    assert portal.resumes[0][1] == _form()
    assert portal.fetches == 0
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)
    assert await _attempt_log(db_pool, invoice_id) == ["captcha_asked", "ok"]
    assert await _count(db_pool, _ROWS, invoice_id) == 0
    assert not image.exists()
    assert len(await _jobs(db_pool, invoice_id, DELIVER_STEP)) == 1


async def test_wrong_answer_with_a_try_left_asks_again_with_br_f20(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209, BR-F20: after captcha_wrong with 2 tries, the next fetch asks a new captcha with BR-F20."""
    invoice_id = await _invoice(db_pool)
    await _attempt_reasons(db_pool, invoice_id, FetchReason.CAPTCHA_ASKED)
    await _answered_row(db_pool, invoice_id, tmp_path)
    portal = FakePortal(FetchReason.CAPTCHA_ASKED, resume=FetchReason.CAPTCHA_WRONG)
    registry: dict[str, Fetcher] = {PORTAL_FETCHER: portal}

    await _run(db_pool, registry, invoice_id, tmp_path)

    assert await _count(db_pool, _ROWS, invoice_id) == 0
    assert await _state(db_pool, invoice_id) == ("FETCHING_DOCS", None)
    (fetch_job,) = await _jobs(db_pool, invoice_id, FETCH_STEP)

    await _run(db_pool, registry, invoice_id, tmp_path, fetch_job)

    assert portal.fetches == 1
    assert await _attempt_log(db_pool, invoice_id) == ["captcha_asked", "captcha_wrong", "captcha_asked"]
    assert await _state(db_pool, invoice_id) == ("WAITING_CAPTCHA", "BR-F20")
    (message,) = await _outbox(db_pool, invoice_id)
    assert message["rule_id"] == "BR-F20"
    assert message["text"] == MESSAGES["BR-F20"]
    assert await _count(db_pool, _ROWS, invoice_id) == 1


async def test_second_wrong_answer_moves_the_chain_to_the_next_fetcher(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: a second captcha_wrong uses up the tries, so the next fetcher runs and no ask is sent."""
    invoice_id = await _invoice(db_pool)
    await _attempt_reasons(
        db_pool, invoice_id, FetchReason.CAPTCHA_ASKED, FetchReason.CAPTCHA_WRONG, FetchReason.CAPTCHA_ASKED
    )
    await _answered_row(db_pool, invoice_id, tmp_path)
    portal = FakePortal(FetchReason.CAPTCHA_ASKED, resume=FetchReason.CAPTCHA_WRONG)
    after = FakeNext()
    registry: dict[str, Fetcher] = {PORTAL_FETCHER: portal, NEXT_FETCHER: after}

    await _run(db_pool, registry, invoice_id, tmp_path)
    (fetch_job,) = await _jobs(db_pool, invoice_id, FETCH_STEP)
    await _run(db_pool, registry, invoice_id, tmp_path, fetch_job)

    assert portal.fetches == 0
    assert after.calls == 1
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)
    assert {message["rule_id"] for message in await _outbox(db_pool, invoice_id)} == {"BR-F16"}
    assert await _count(db_pool, _ROWS, invoice_id) == 0


async def test_portal_without_a_captcha_fetches_as_before(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D209: with no captcha row the portal's plain fetch runs, resume is not called, nothing is asked."""
    invoice_id = await _invoice(db_pool)
    portal = FakePortal(FetchReason.OK)

    await _run(db_pool, {PORTAL_FETCHER: portal}, invoice_id, tmp_path)

    assert (portal.fetches, portal.resumes) == (1, [])
    assert await _state(db_pool, invoice_id) == ("DELIVERING", None)
    assert await _attempt_log(db_pool, invoice_id) == ["ok"]
    assert await _count(db_pool, _ROWS, invoice_id) == 0
    assert await _jobs(db_pool, invoice_id, CAPTCHA_TIMEOUT_STEP) == []
    assert not (tmp_path / "captchas").exists()
