"""Integration tests for the extract handler and its helper process.

Every test runs the real ``python -m invoicebot.extract`` helper on an image or PDF built in
the test (R-2.1, R-2.13, R-2.15, BR-E01, BR-E02, BR-E05, R-9.4). The database is real Postgres.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import hashlib
import logging
import os
import shlex
import signal
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import cv2
import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as raw
import pytest
import pytest_asyncio
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from invoicebot.channels.base import AttachmentKind
from invoicebot.config.loader import load_config
from invoicebot.config.models import Extraction, MediaLimits, MediaMaxBytes, Ocr
from invoicebot.extract.fields import FULL_CONFIDENCE, REQUIRED, Field, Reading, reading_to_json
from invoicebot.extract.qr_payload import parse_qr_payload
from invoicebot.extract.validate import is_valid_ruc
from invoicebot.gaps.kinds import fingerprint
from invoicebot.pipeline import extract as extract_module
from invoicebot.pipeline.extract import (
    MAX_HELPER_OUTPUT_BYTES,
    ROUTE_STEP,
    _HelperResult,
    _parse_result,
    make_extract_handler,
)
from invoicebot.pipeline.gaps import RULE_GAP_ADMIN, GapContext
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.queue.jobs import Job
from invoicebot.store.invoices import FileRecord, add_file, create_invoice, load_reading

pytestmark = pytest.mark.asyncio(loop_scope="session")

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"
#: The same number with its last digit changed, so the check digit is deliberately wrong.
BAD_CHECK_DIGIT_RUC = "20000000002"
SERIE = "F001"
NUMERO = "15160"
IGV = "7.36"
TOTAL = "49.90"
FECHA = "2026-11-13"
UNSUPPORTED_TIPO = "09"
VALID_PAYLOAD = f"{SYNTHETIC_RUC}|01|{SERIE}|{NUMERO}|{IGV}|{TOTAL}|{FECHA}|6|{SYNTHETIC_RUC}"
BAD_RUC_PAYLOAD = f"{BAD_CHECK_DIGIT_RUC}|01|{SERIE}|{NUMERO}|{IGV}|{TOTAL}|{FECHA}|6|{SYNTHETIC_RUC}"
UNSUPPORTED_TIPO_PAYLOAD = (
    f"{SYNTHETIC_RUC}|{UNSUPPORTED_TIPO}|{SERIE}|{NUMERO}|{IGV}|{TOTAL}|{FECHA}|6|{SYNTHETIC_RUC}"
)
WHITE = 255
RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "parsing_rules.yaml"
EXAMPLE_CONFIG = RULES_PATH.parent / "example.yaml"
CANVAS = 800  # height and width, in pixels
QR_SCALE = 6
QR_TOP = 100
QR_LEFT = 100
PNG = ".png"
PDF_TEXT_LEFT = 40
PDF_FIRST_LINE_TOP = 700
PDF_LINE_HEIGHT = 50
PDF_FONT_SIZE = 14
PDF_TEXT_PAGE = (600, 800)  # width, height
PDF_BLANK_PAGE = (200, 200)
#: A PDF byte cap small enough that a padded invoice exceeds it (R-1.11).
PDF_BYTE_CAP = 2 * 1024 * 1024
OCR_TIMEOUT_S = 30
HELPER_TIMEOUT_S = 50
#: A recent date, so an invoice read from the photo still routes (BR-E03).
PRINTED_DATE = datetime.now(UTC).date()
#: The six required fields as printed text on the photo (R-2.3, D81).
PRINTED_LINES = (
    f"RUC {SYNTHETIC_RUC}",
    "FACTURA ELECTRONICA",
    f"{SERIE}-{NUMERO}",
    f"Fecha de emision: {PRINTED_DATE:%d/%m/%Y}",
    f"IMPORTE TOTAL S/ {TOTAL}",
)
#: A page-1 text layer carrying the six required fields (R-2.15, D122).
PDF_TEXT_LINES = (
    f"RUC: {SYNTHETIC_RUC}",
    f"FACTURA ELECTRONICA {SERIE}-{NUMERO}",
    f"Fecha de emision: {PRINTED_DATE:%d/%m/%Y}",
    f"IMPORTE TOTAL S/ {TOTAL}",
)
#: The OCR total when the printed value disagrees with the QR (D81).
CONFLICT_TOTAL = "12.50"
#: The synthetic razón social the seeded padrón row carries (R-2.5).
PADRON_RAZON_SOCIAL = "EMPRESA DE PRUEBA"
#: The synthetic razón social the fake OCR reading carries, which the padrón replaces (R-2.5).
OCR_RAZON_SOCIAL = "EMPRESA LEIDA"
#: The gap two unreadable invoices from SYNTHETIC_RUC share (BR-E06, D115).
PARSER_MISS_FINGERPRINT = fingerprint("PARSER_MISS", SYNTHETIC_RUC)
#: A QR payload carrying the same values as PRINTED_LINES (D81).
CONFIRMING_PAYLOAD = (
    f"{SYNTHETIC_RUC}|01|{SERIE}|{NUMERO}|{IGV}|{TOTAL}|{PRINTED_DATE.isoformat()}|6|{SYNTHETIC_RUC}"
)
EXPIRED_TIMEOUT_S = 0.001  # far below any decode, so the helper always times out
SLOW_HELPER_SCRIPT = "import time; time.sleep(30)"
#: A helper that prints more than the output cap and exits 0 (R-9.2).
OVERSIZED_HELPER_SCRIPT = f"import sys; sys.stdout.write('x' * {MAX_HELPER_OUTPUT_BYTES + 1})"
#: A helper that starts a sleeping grandchild, records its pid, then sleeps (R-9.2).
#: The grandchild must not hold the helper's stdout pipe, or reading stdout to EOF
#: would keep the handler waiting until the grandchild exits.
TREE_HELPER_SCRIPT = (
    "import subprocess, sys, time\n"
    "child = subprocess.Popen(\n"
    "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
    "    stdout=subprocess.DEVNULL,\n"
    "    stderr=subprocess.DEVNULL,\n"
    ")\n"
    "with open(sys.argv[1], 'w') as pid_file:\n"
    "    pid_file.write(str(child.pid))\n"
    "time.sleep(30)\n"
)
#: Long enough for the grandchild to start, short enough to keep the test quick.
TREE_TIMEOUT_S = 2.0
#: How long a killed grandchild may take to be reparented and reaped by init.
GRANDCHILD_GONE_TIMEOUT_S = 3.0
GRANDCHILD_POLL_S = 0.05
CHILD_START_TIMEOUT_S = 10
SIGKILL_EXIT_CODE = -9


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _empty_padron(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
) -> AsyncIterator[None]:
    """Start each test from an empty padrón, so no row from another test fills a reading (R-2.5)."""
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM padron")
    yield


#: The media limits of config/example.yaml (R-1.11).
MEDIA = MediaLimits(
    max_megapixels=40,
    max_bytes=MediaMaxBytes(image=10_485_760, pdf=10_485_760, xml=2_097_152, zip=20_971_520),
    zip_max_entries=20,
    zip_max_ratio=50,
)


def _config(*, helper_timeout_s: float = HELPER_TIMEOUT_S) -> Extraction:
    """Return the extraction config the handler runs with.

    The timeout tests deliberately use a helper timeout at or below the OCR timeout, which the
    model refuses (D81); ``model_copy`` sets it there without revalidation.
    """
    config = Extraction(
        max_image_px=2000,
        ocr=Ocr(engine="tesseract", lang="spa", psm=4, timeout_s=OCR_TIMEOUT_S),
        review_threshold=0.8,
        max_invoice_age_days=365,
        igv_rates=[Decimal("0.18"), Decimal("0.105"), Decimal("0.10")],
        supported_tipos=["01", "03", "07", "08"],
        helper_timeout_s=HELPER_TIMEOUT_S,
    )
    if helper_timeout_s == HELPER_TIMEOUT_S:
        return config
    return config.model_copy(update={"helper_timeout_s": helper_timeout_s})


def _gap_context() -> GapContext:
    """Return the example config's profiles and gaps settings, PARSER_MISS a digest kind."""
    cfg = load_config(EXAMPLE_CONFIG)
    return GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps)


def _canvas() -> np.ndarray:
    """Return a white canvas the size of a photo."""
    return np.full((CANVAS, CANVAS), WHITE, dtype=np.uint8)


def _write_blank_photo(path: Path) -> Path:
    """Write a white photo with no QR and return its path."""
    return _write_image(_canvas(), path)


def _draw_qr(canvas: np.ndarray, payload: str) -> None:
    """Draw ``payload`` as a QR code at the photo's top-left."""
    code = cv2.QRCodeEncoder.create().encode(payload)
    code = cv2.resize(code, None, fx=QR_SCALE, fy=QR_SCALE, interpolation=cv2.INTER_NEAREST)
    height, width = code.shape
    canvas[QR_TOP : QR_TOP + height, QR_LEFT : QR_LEFT + width] = code


def _write_qr_photo(payload: str, path: Path) -> Path:
    """Write a photo carrying ``payload`` as a QR code and return its path."""
    canvas = _canvas()
    _draw_qr(canvas, payload)
    return _write_image(canvas, path)


def _write_printed_photo(
    lines: Sequence[str],
    path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
    *,
    qr_payload: str | None = None,
) -> Path:
    """Write a photo carrying ``lines`` as printed text, with a QR code when given."""
    canvas = _canvas()
    if qr_payload is not None:
        _draw_qr(canvas, qr_payload)
    draw_text(canvas, lines)
    return _write_image(canvas, path)


def _write_image(canvas: np.ndarray, path: Path) -> Path:
    """Encode ``canvas`` as a PNG at ``path``."""
    ok, encoded = cv2.imencode(PNG, canvas)
    assert ok
    path.write_bytes(encoded.tobytes())
    return path


def _add_pdf_text(page: pdfium.PdfPage, document: pdfium.PdfDocument, text: str, top: int) -> None:
    """Draw ``text`` on ``page`` as real PDF text, so it lands in the page's text layer (R-2.15)."""
    font = raw.FPDFText_LoadStandardFont(document.raw, b"Helvetica")
    text_object = raw.FPDFPageObj_CreateTextObj(document.raw, font, PDF_FONT_SIZE)
    encoded = (text + "\x00").encode("utf-16-le")
    raw.FPDFText_SetText(text_object, ctypes.cast(encoded, raw.FPDF_WIDESTRING))
    raw.FPDFPageObj_Transform(text_object, 1, 0, 0, 1, PDF_TEXT_LEFT, top)
    raw.FPDFPage_InsertObject(page.raw, text_object)


def _write_text_pdf(path: Path) -> Path:
    """Write a one-page PDF whose lines are drawn with pdfium's text API (R-2.15)."""
    document = pdfium.PdfDocument.new()
    page = document.new_page(width=PDF_TEXT_PAGE[0], height=PDF_TEXT_PAGE[1])
    for index, line in enumerate(PDF_TEXT_LINES):
        _add_pdf_text(page, document, line, PDF_FIRST_LINE_TOP - index * PDF_LINE_HEIGHT)
    page.gen_content()
    document.save(str(path))
    document.close()
    return path


def _write_blank_pdf(path: Path, pages: int) -> Path:
    """Write a PDF with ``pages`` blank pages (R-2.15)."""
    document = pdfium.PdfDocument.new()
    for _ in range(pages):
        document.new_page(width=PDF_BLANK_PAGE[0], height=PDF_BLANK_PAGE[1])
    document.save(str(path))
    document.close()
    return path


def _job(invoice_id: int) -> Job:
    """Build the extract job of invoice_id; the handler only uses its invoice id."""
    return Job(
        id=1,
        invoice_id=invoice_id,
        step=EXTRACT_STEP,
        attempts=0,
        crashes=0,
        lease_until=datetime.now(UTC),
    )


async def _invoice_with_file(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    path: Path,
    kind: AttachmentKind,
) -> tuple[int, str]:
    """Create a RECEIVED invoice with one file row of ``kind`` and return it with its profile."""
    profile_id = f"profile-{uuid4().hex}"
    data = await asyncio.to_thread(path.read_bytes)
    record = FileRecord(
        kind=kind,
        local_path=str(path),
        remote_ref=None,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    async with pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await add_file(conn, invoice_id, record)
    return invoice_id, profile_id


async def _invoice_with_photo(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    photo_path: Path,
) -> tuple[int, str]:
    """Create a RECEIVED invoice with one photo file row and return it with its profile."""
    return await _invoice_with_file(pool, photo_path, "photo")


def _media_with_pdf_cap(pdf_cap: int) -> MediaLimits:
    """Return the media limits with ``max_bytes.pdf`` set to ``pdf_cap`` (R-1.11)."""
    return MEDIA.model_copy(update={"max_bytes": MEDIA.max_bytes.model_copy(update={"pdf": pdf_cap})})


async def _run_handler(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    *,
    helper_timeout_s: float = HELPER_TIMEOUT_S,
    media: MediaLimits = MEDIA,
    helper_prefix: Sequence[str] = (),
) -> None:
    """Run the extract handler once for invoice_id."""
    await make_extract_handler(
        pool,
        _config(helper_timeout_s=helper_timeout_s),
        media,
        RULES_PATH,
        _gap_context(),
        helper_prefix=helper_prefix,
    )(_job(invoice_id))


def _ocr_reading() -> Reading:
    """Return a valid reading with an OCR razón social the padrón can replace (R-2.5)."""
    reading = parse_qr_payload(VALID_PAYLOAD)
    assert reading is not None
    return replace(
        reading,
        fields={**reading.fields, "razon_social": Field(OCR_RAZON_SOCIAL, "ocr", FULL_CONFIDENCE)},
    )


async def _run_handler_with_reading(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    invoice_id: int,
    reading: Reading,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the extract handler once with ``reading`` in place of the helper's output (R-2.5)."""
    result = _HelperResult(
        reading=reading_to_json(reading),
        variant=None,
        decoder=None,
        ocr=True,
        error=None,
    )

    async def _fake_run_helper(*_args: Any, **_kwargs: Any) -> _HelperResult:
        return result

    monkeypatch.setattr(extract_module, "_run_helper", _fake_run_helper)
    await _run_handler(pool, invoice_id)


async def _seed_padron_row(pool: AsyncConnectionPool[AsyncConnection[Any]], razon_social: str) -> None:
    """Seed the fake reading's issuer in the padrón with ``razon_social`` (R-2.5)."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO padron (ruc, razon_social, estado, condicion) VALUES (%s, %s, %s, %s)",
            (int(SYNTHETIC_RUC), razon_social, "ACTIVO", "HABIDO"),
        )


def _record_text(record: logging.LogRecord) -> str:
    """Return the record's message and every attribute, extras included, as one string."""
    return " ".join([record.getMessage(), *(str(value) for value in vars(record).values())])


async def _invoice_row(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, str | None]:
    """Return ``(state, rule_id)`` of an invoice."""
    cur = await conn.execute("SELECT state, rule_id FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0]), None if row[1] is None else str(row[1])


async def _job_count(conn: AsyncConnection[Any], invoice_id: int, step: str) -> int:
    """Return how many jobs of ``step`` the invoice has."""
    cur = await conn.execute(
        "SELECT count(*) FROM jobs WHERE invoice_id = %s AND step = %s",
        (invoice_id, step),
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _outbox_rows(conn: AsyncConnection[Any], profile_id: str, rule_id: str) -> list[str]:
    """Return the texts of the outbox rows for ``profile_id`` under ``rule_id``."""
    cur = await conn.execute(
        """
        SELECT payload_json->>'text'
        FROM outbox
        WHERE "to" = %s AND payload_json->>'rule_id' = %s
        """,
        (profile_id, rule_id),
    )
    return [str(text) for (text,) in await cur.fetchall()]


async def _clear_gaps(pool: AsyncConnectionPool[AsyncConnection[Any]]) -> None:
    """Start a gap test from no gaps, no invoice links and an empty outbox."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM invoice_gaps")
        await conn.execute("DELETE FROM gaps")
        await conn.execute("DELETE FROM outbox")


async def _gap_state(conn: AsyncConnection[Any], gap_fingerprint: str) -> tuple[int, datetime | None] | None:
    """Return a gap's count and raised_at, or None when it has no row."""
    cur = await conn.execute("SELECT count, raised_at FROM gaps WHERE fingerprint = %s", (gap_fingerprint,))
    row = await cur.fetchone()
    return None if row is None else (int(row[0]), row[1])


async def _gap_admin_rows(conn: AsyncConnection[Any]) -> int:
    """Return how many GAP_admin owner alerts the outbox holds."""
    cur = await conn.execute(
        "SELECT count(*) FROM outbox WHERE payload_json->>'rule_id' = %s", (RULE_GAP_ADMIN,)
    )
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def _unreadable_photo_with_issuer_ruc(
    pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
    name: str,
) -> int:
    """Create an invoice whose photo shows only the issuer RUC, so BR-E01 keeps the reading."""
    photo = _write_printed_photo((f"RUC {SYNTHETIC_RUC}",), tmp_path / f"{name}{PNG}", draw_text)
    invoice_id, _profile_id = await _invoice_with_photo(pool, photo)
    return invoice_id


async def test_valid_qr_photo_saves_the_reading_and_queues_routing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-2.1, R-2.13, R-9.4: a valid QR photo ends in ROUTING with one route job and the reading saved."""
    invoice_id, profile_id = await _invoice_with_photo(
        db_pool, _write_qr_photo(VALID_PAYLOAD, tmp_path / f"invoice{PNG}")
    )

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        unreadable = await _outbox_rows(conn, profile_id, "BR-E01")

    assert (state, rule_id) == ("ROUTING", None)
    assert reading == parse_qr_payload(VALID_PAYLOAD)
    assert route_jobs == 1
    assert unreadable == []


def _write_prefix_script(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a fake helper prefix that records its argv and stdin then runs the helper (D147, D158).

    Returns the script, the marker file it appends each argument to, and the file it copies
    stdin to before piping the same bytes on to the helper.
    """
    marker = tmp_path / "prefix-argv.txt"
    stdin_path = tmp_path / "prefix-stdin.bin"
    script = tmp_path / "sandbox-prefix.sh"
    script.write_text(
        f"#!/bin/sh\nprintf '%s\\n' \"$@\" >> {shlex.quote(str(marker))}\n"
        f'tee {shlex.quote(str(stdin_path))} | "$@"\n'
    )
    script.chmod(0o755)
    return script, marker, stdin_path


async def test_helper_prefix_wraps_the_extract_helper(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """D147, D158: the prefix runs before the helper, which receives the photo bytes on stdin."""
    photo = _write_qr_photo(VALID_PAYLOAD, tmp_path / f"prefixed{PNG}")
    invoice_id, profile_id = await _invoice_with_photo(db_pool, photo)
    script, marker, stdin_path = _write_prefix_script(tmp_path)

    await _run_handler(db_pool, invoice_id, helper_prefix=(str(script),))

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        unreadable = await _outbox_rows(conn, profile_id, "BR-E01")

    assert (state, rule_id) == ("ROUTING", None)
    assert reading == parse_qr_payload(VALID_PAYLOAD)
    assert route_jobs == 1
    assert unreadable == []
    recorded = marker.read_text().splitlines()
    assert str(photo) not in recorded
    assert "-m" in recorded
    assert recorded[recorded.index("-m") + 1] == "invoicebot.extract"
    assert stdin_path.read_bytes() == photo.read_bytes()


async def test_text_only_photo_routes_with_ocr_fields(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
) -> None:
    """R-2.3: a photo printing the six required fields with no QR ends in ROUTING with OCR fields."""
    photo = _write_printed_photo(PRINTED_LINES, tmp_path / f"printed{PNG}", draw_text)
    invoice_id, _ = await _invoice_with_photo(db_pool, photo)

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)

    assert (state, rule_id) == ("ROUTING", None)
    assert reading is not None
    assert reading.missing_required() == ()
    assert all(field.source == "ocr" for field in reading.fields.values())
    assert reading.value("ruc_emisor") == SYNTHETIC_RUC
    assert reading.value("fecha_emision") == PRINTED_DATE.isoformat()
    assert reading.value("total") == TOTAL


async def test_seeded_padron_replaces_the_ocr_razon_social(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-2.5: a seeded padrón row replaces the OCR razón social with source ``padron``."""
    await _seed_padron_row(db_pool, PADRON_RAZON_SOCIAL)
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"padron{PNG}"))

    await _run_handler_with_reading(db_pool, invoice_id, _ocr_reading(), monkeypatch)

    async with db_pool.connection() as conn:
        reading = await load_reading(conn, invoice_id)

    assert reading is not None
    razon_social = reading.fields["razon_social"]
    assert (razon_social.value, razon_social.source, razon_social.confidence) == (
        PADRON_RAZON_SOCIAL,
        "padron",
        FULL_CONFIDENCE,
    )


async def test_empty_padron_keeps_the_ocr_razon_social(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-2.5: an empty padrón keeps the OCR razón social as the fallback."""
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"no-padron{PNG}"))

    await _run_handler_with_reading(db_pool, invoice_id, _ocr_reading(), monkeypatch)

    async with db_pool.connection() as conn:
        reading = await load_reading(conn, invoice_id)

    assert reading is not None
    razon_social = reading.fields["razon_social"]
    assert (razon_social.value, razon_social.source) == (OCR_RAZON_SOCIAL, "ocr")


async def test_extraction_logs_carry_no_razon_social(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-9.16: the extraction log lines carry neither the OCR nor the padrón razón social."""
    caplog.set_level(logging.DEBUG)
    await _seed_padron_row(db_pool, PADRON_RAZON_SOCIAL)
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"secret{PNG}"))

    await _run_handler_with_reading(db_pool, invoice_id, _ocr_reading(), monkeypatch)

    assert any(getattr(record, "step", None) == EXTRACT_STEP for record in caplog.records)
    leaks = [
        (record.name, value)
        for record in caplog.records
        for value in (PADRON_RAZON_SOCIAL, OCR_RAZON_SOCIAL)
        if value in _record_text(record)
    ]
    assert leaks == []


async def test_qr_and_printed_text_confirm_each_other(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
) -> None:
    """D81: a QR and the same values printed store the required fields at confidence 1.0."""
    photo = _write_printed_photo(
        PRINTED_LINES, tmp_path / f"confirmed{PNG}", draw_text, qr_payload=CONFIRMING_PAYLOAD
    )
    invoice_id, _ = await _invoice_with_photo(db_pool, photo)

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)

    assert (state, rule_id) == ("ROUTING", None)
    assert reading is not None
    assert reading.missing_required() == ()
    assert all(reading.fields[name].confidence == FULL_CONFIDENCE for name in REQUIRED)
    assert all(reading.fields[name].flag is None for name in REQUIRED)


async def test_printed_total_disagreeing_with_qr_is_flagged_conflict(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
) -> None:
    """D81: a printed total that disagrees with the QR routes with the OCR total flagged conflict."""
    lines = (*PRINTED_LINES[:-1], f"IMPORTE TOTAL S/ {CONFLICT_TOTAL}")
    photo = _write_printed_photo(lines, tmp_path / f"conflict{PNG}", draw_text, qr_payload=CONFIRMING_PAYLOAD)
    invoice_id, _ = await _invoice_with_photo(db_pool, photo)

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)

    assert (state, rule_id) == ("ROUTING", None)
    assert reading is not None
    total = reading.fields["total"]
    assert (total.value, total.source, total.flag) == (CONFLICT_TOTAL, "ocr", "conflict")


async def test_blank_photo_fails_with_br_e01(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-E01: a photo with no QR ends in EXTRACT_FAILED with one BR-E01 outbox row."""
    invoice_id, profile_id = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"blank{PNG}"))

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E01")

    assert (state, rule_id) == ("EXTRACT_FAILED", "BR-E01")
    assert route_jobs == 0
    assert len(messages) == 1
    assert "No pude leer este comprobante" in messages[0]


async def test_two_unreadable_photos_raise_parser_miss_on_the_second(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
) -> None:
    """BR-E06: two BR-E01 photos from one issuer count the gap, and the second raises it.

    PARSER_MISS is a digest kind, so neither photo queues a GAP_admin owner alert.
    """
    await _clear_gaps(db_pool)
    first = await _unreadable_photo_with_issuer_ruc(db_pool, tmp_path, draw_text, "parser-miss-1")

    await _run_handler(db_pool, first)

    async with db_pool.connection() as conn:
        after_first = await _gap_state(conn, PARSER_MISS_FINGERPRINT)
        alerts_after_first = await _gap_admin_rows(conn)
        first_state = await _invoice_row(conn, first)

    assert after_first is not None
    assert (after_first[0], after_first[1]) == (1, None)
    assert alerts_after_first == 0
    assert first_state == ("EXTRACT_FAILED", "BR-E01")

    second = await _unreadable_photo_with_issuer_ruc(db_pool, tmp_path, draw_text, "parser-miss-2")

    await _run_handler(db_pool, second)

    async with db_pool.connection() as conn:
        after_second = await _gap_state(conn, PARSER_MISS_FINGERPRINT)
        alerts_after_second = await _gap_admin_rows(conn)
        second_state = await _invoice_row(conn, second)

    assert after_second is not None
    assert after_second[0] == 2
    assert after_second[1] is not None
    assert alerts_after_second == 0
    assert second_state == ("EXTRACT_FAILED", "BR-E01")


async def test_one_unreadable_photo_raises_no_parser_miss_gap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    draw_text: Callable[[np.ndarray, Sequence[str]], None],
) -> None:
    """BR-E06: one BR-E01 photo counts the gap but raises it and alerts nobody."""
    await _clear_gaps(db_pool)
    invoice_id = await _unreadable_photo_with_issuer_ruc(db_pool, tmp_path, draw_text, "parser-miss-one")

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        gap = await _gap_state(conn, PARSER_MISS_FINGERPRINT)
        alerts = await _gap_admin_rows(conn)

    assert gap is not None
    assert (gap[0], gap[1]) == (1, None)
    assert alerts == 0


async def test_unreadable_photo_without_a_ruc_adds_no_parser_miss_gap(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-E06: a BR-E01 photo that shows no issuer RUC adds no gap row and alerts nobody."""
    await _clear_gaps(db_pool)
    invoice_id, _profile_id = await _invoice_with_photo(
        db_pool, _write_blank_photo(tmp_path / f"parser-miss-no-ruc{PNG}")
    )

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        gap = await _gap_state(conn, PARSER_MISS_FINGERPRINT)
        alerts = await _gap_admin_rows(conn)
        state = await _invoice_row(conn, invoice_id)

    assert gap is None
    assert alerts == 0
    assert state == ("EXTRACT_FAILED", "BR-E01")


async def test_bad_issuer_check_digit_fails_with_br_e02(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-E02: a QR whose issuer RUC has a bad check digit ends in EXTRACT_FAILED with BR-E02."""
    assert not is_valid_ruc(BAD_CHECK_DIGIT_RUC)
    invoice_id, profile_id = await _invoice_with_photo(
        db_pool, _write_qr_photo(BAD_RUC_PAYLOAD, tmp_path / f"bad-ruc{PNG}")
    )

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E02")

    assert (state, rule_id) == ("EXTRACT_FAILED", "BR-E02")
    assert route_jobs == 0
    assert len(messages) == 1
    assert BAD_CHECK_DIGIT_RUC in messages[0]


async def test_unsupported_tipo_cancels_with_br_e05(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-E05: a QR whose tipo is outside supported_tipos ends in CANCELLED with BR-E05."""
    invoice_id, profile_id = await _invoice_with_photo(
        db_pool, _write_qr_photo(UNSUPPORTED_TIPO_PAYLOAD, tmp_path / f"tipo{PNG}")
    )

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E05")

    assert (state, rule_id) == ("CANCELLED", "BR-E05")
    assert route_jobs == 0
    assert len(messages) == 1
    assert "Solo proceso facturas y boletas electrónicas" in messages[0]


async def test_missing_file_raises_so_the_queue_retries(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4: a photo path that is not on disk raises, leaving the invoice for a retry."""
    missing = tmp_path / "gone.png"
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"real{PNG}"))
    async with db_pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE files SET local_path = %s WHERE invoice_id = %s",
            (str(missing), invoice_id),
        )

    with pytest.raises(RuntimeError):
        await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)

    assert (state, rule_id) == ("EXTRACTING", None)
    assert route_jobs == 0


async def test_file_over_the_media_cap_raises_unread_so_the_queue_retries(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-1.11: a photo larger than the image cap is refused before it is read or sent to the helper."""
    photo = _write_blank_photo(tmp_path / f"big{PNG}")
    invoice_id, _ = await _invoice_with_photo(db_pool, photo)
    tiny = MEDIA.model_copy(update={"max_bytes": MEDIA.max_bytes.model_copy(update={"image": 1})})

    with pytest.raises(RuntimeError, match="exceeds the media cap"):
        await _run_handler(db_pool, invoice_id, media=tiny)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
    assert (state, rule_id) == ("EXTRACTING", None)


async def test_helper_timeout_is_killed_and_fails_with_br_e01(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """BR-E01: a helper that outlives helper_timeout_s is killed and the invoice fails with BR-E01."""
    invoice_id, profile_id = await _invoice_with_photo(
        db_pool, _write_qr_photo(VALID_PAYLOAD, tmp_path / f"slow{PNG}")
    )

    await _run_handler(db_pool, invoice_id, helper_timeout_s=EXPIRED_TIMEOUT_S)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E01")

    assert (state, rule_id) == ("EXTRACT_FAILED", "BR-E01")
    assert route_jobs == 0
    assert len(messages) == 1


async def test_second_run_on_a_routed_invoice_changes_nothing(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-9.4: running the handler twice on a routed invoice adds no job and keeps the state."""
    invoice_id, profile_id = await _invoice_with_photo(
        db_pool, _write_qr_photo(VALID_PAYLOAD, tmp_path / f"twice{PNG}")
    )
    await _run_handler(db_pool, invoice_id)

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)

    assert (state, rule_id) == ("ROUTING", None)
    assert reading == parse_qr_payload(VALID_PAYLOAD)
    assert route_jobs == 1
    assert await _outbox_count(db_pool, profile_id) == 0


async def _outbox_count(pool: AsyncConnectionPool[AsyncConnection[Any]], profile_id: str) -> int:
    """Return how many outbox rows the profile has."""
    async with pool.connection() as conn:
        cur = await conn.execute('SELECT count(*) FROM outbox WHERE "to" = %s', (profile_id,))
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


async def test_unreadable_pdf_fails_with_br_e01_without_raising(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-2.15, BR-E01: an invoice whose PDF is not readable ends in EXTRACT_FAILED with BR-E01."""
    profile_id = f"profile-{uuid4().hex}"
    pdf_path = tmp_path / "invoice.pdf"
    data = b"%PDF-1.4 synthetic"
    pdf_path.write_bytes(data)
    record = FileRecord(
        kind="pdf",
        local_path=str(pdf_path),
        remote_ref=None,
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
    )
    async with db_pool.connection() as conn, conn.transaction():
        invoice_id = await create_invoice(conn, profile_id, "whatsapp")
        await add_file(conn, invoice_id, record)

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E01")

    assert (state, rule_id) == ("EXTRACT_FAILED", "BR-E01")
    assert route_jobs == 0
    assert len(messages) == 1
    assert "No pude leer este comprobante" in messages[0]


async def test_text_pdf_is_read_and_routes(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-2.15: a text PDF ends in ROUTING with a reading taken from its text layer."""
    invoice_id, _ = await _invoice_with_file(db_pool, _write_text_pdf(tmp_path / "invoice.pdf"), "pdf")

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        reading = await load_reading(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)

    assert (state, rule_id) == ("ROUTING", None)
    assert route_jobs == 1
    assert reading is not None
    assert reading.missing_required() == ()
    assert reading.value("ruc_emisor") == SYNTHETIC_RUC
    assert reading.value("serie") == SERIE
    assert reading.value("numero") == NUMERO
    assert reading.value("fecha_emision") == PRINTED_DATE.isoformat()
    assert reading.value("total") == TOTAL
    assert reading.fields["ruc_emisor"].source == "pdf"


async def test_pdf_over_the_page_cap_cancels_with_br_e07(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]], tmp_path: Path
) -> None:
    """R-2.15: a PDF over max_pdf_pages ends CANCELLED with BR-E07 naming the page cap."""
    pdf = _write_blank_pdf(tmp_path / "long.pdf", MEDIA.max_pdf_pages + 1)
    invoice_id, profile_id = await _invoice_with_file(db_pool, pdf, "pdf")

    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)
        messages = await _outbox_rows(conn, profile_id, "BR-E07")

    assert (state, rule_id) == ("CANCELLED", "BR-E07")
    assert route_jobs == 0
    assert len(messages) == 1
    assert f"tiene más de {MEDIA.max_pdf_pages} páginas" in messages[0]


async def test_cancel_between_read_and_extracting_wins(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D95, D96: a /cancelar committed after the handler read the invoice stops it before the helper."""
    invoice_id, _ = await _invoice_with_photo(
        db_pool, _write_qr_photo(VALID_PAYLOAD, tmp_path / f"race{PNG}")
    )
    real_get_file_path = extract_module.get_file_path
    helper_calls: list[tuple[Any, ...]] = []

    async def _get_file_path_then_cancel(conn: AsyncConnection[Any], *args: Any) -> Path | None:
        path = await real_get_file_path(conn, *args)
        async with db_pool.connection() as other, other.transaction():
            await other.execute("UPDATE invoices SET state = 'CANCELLED' WHERE id = %s", (invoice_id,))
        return path

    async def _no_exec(*args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        helper_calls.append(args)
        raise AssertionError("the helper must not run")

    monkeypatch.setattr(extract_module, "get_file_path", _get_file_path_then_cancel)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _no_exec)
    await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, _rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)

    assert state == "CANCELLED"
    assert route_jobs == 0
    assert helper_calls == []


async def test_cancelling_the_handler_leaves_no_helper_process(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-9.2: cancelling the handler while the helper runs kills and reaps the helper."""
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"cancel{PNG}"))
    started = asyncio.Event()
    children: list[asyncio.subprocess.Process] = []
    real_exec = asyncio.create_subprocess_exec

    async def _slow_exec(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        child = await real_exec(
            sys.executable,
            "-c",
            SLOW_HELPER_SCRIPT,
            stdin=kwargs.get("stdin", asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=kwargs.get("start_new_session", False),
        )
        children.append(child)
        started.set()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _slow_exec)
    task = asyncio.create_task(_run_handler(db_pool, invoice_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=CHILD_START_TIMEOUT_S)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert [child.returncode for child in children] == [SIGKILL_EXIT_CODE]
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)


async def test_timeout_kills_a_helper_and_the_child_it_started(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-9.2: a helper that started a child is killed on timeout and the child is gone too."""
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"tree{PNG}"))
    pid_file = tmp_path / "grandchild.pid"
    real_exec = asyncio.create_subprocess_exec

    async def _tree_exec(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return await real_exec(
            sys.executable,
            "-c",
            TREE_HELPER_SCRIPT,
            str(pid_file),
            stdin=kwargs.get("stdin", asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _tree_exec)
    grandchild_pid: int | None = None
    try:
        await _run_handler(db_pool, invoice_id, helper_timeout_s=TREE_TIMEOUT_S)
        assert pid_file.exists()
        grandchild_pid = int(pid_file.read_text())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GRANDCHILD_GONE_TIMEOUT_S
        while True:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            if loop.time() >= deadline:
                pytest.fail(f"grandchild {grandchild_pid} outlived the helper")
            await asyncio.sleep(GRANDCHILD_POLL_S)
    finally:
        if grandchild_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)


async def test_helper_output_over_the_cap_raises_so_the_queue_retries(
    db_pool: AsyncConnectionPool[AsyncConnection[Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-9.2: a helper over the cap raises RuntimeError and leaves the invoice for a retry."""
    invoice_id, _ = await _invoice_with_photo(db_pool, _write_blank_photo(tmp_path / f"oversized{PNG}"))
    real_exec = asyncio.create_subprocess_exec

    async def _oversized_exec(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        return await real_exec(
            sys.executable,
            "-c",
            OVERSIZED_HELPER_SCRIPT,
            stdin=kwargs.get("stdin", asyncio.subprocess.DEVNULL),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=kwargs.get("start_new_session", False),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _oversized_exec)

    with pytest.raises(RuntimeError, match="output exceeds the cap"):
        await _run_handler(db_pool, invoice_id)

    async with db_pool.connection() as conn:
        state, rule_id = await _invoice_row(conn, invoice_id)
        route_jobs = await _job_count(conn, invoice_id, ROUTE_STEP)

    assert (state, rule_id) == ("EXTRACTING", None)
    assert route_jobs == 0


async def test_malformed_helper_output_drops_the_pydantic_cause() -> None:
    """R-9.16: malformed output raises RuntimeError without the pydantic text that quotes values."""
    with pytest.raises(RuntimeError, match="malformed JSON") as caught:
        _parse_result(b'{"reading": {"fields": {"ruc_emisor": {"value": "20000000001"}}}}')

    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
