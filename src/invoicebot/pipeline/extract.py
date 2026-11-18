"""Job handler that reads one invoice photo or PDF in a helper process (S3.11, R-2.1, R-2.15).

:func:`make_extract_handler` builds the handler of the ``extract`` job step. QR decoding and
OCR run in ``python -m invoicebot.extract`` under the heavy-work slot, so the bot process
never loads OpenCV, zxing, Tesseract or pypdfium2 and their memory returns when the helper
exits (D78, D122). The file's bytes go to the helper on stdin, so it never opens a path under
the data root (D158). The helper's one JSON line is validated before use. The outcome's state
change, next job and outbox message commit in one transaction (CLAUDE.md "Database").
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, ValidationError

from invoicebot.channels.base import AttachmentKind
from invoicebot.channels.media_limits import max_bytes_for
from invoicebot.config.models import Extraction, MediaLimits
from invoicebot.extract.fields import Reading, reading_from_json
from invoicebot.extract.names import QrDecoder, QrVariant
from invoicebot.extract.padron import with_padron
from invoicebot.extract.validate import RULE_BAD_RUC, RULE_UNSUPPORTED_TIPO, check_issuer, check_tipo
from invoicebot.gaps.kinds import fingerprint
from invoicebot.github.issue_text import IssueFields
from invoicebot.helper import run_helper
from invoicebot.pipeline.gaps import GapContext, GapSighting, raise_gap
from invoicebot.pipeline.handlers import (
    EXTRACT_STEP,
    RULE_MEDIA_REJECTED,
    MediaRejection,
    reject_media,
)
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import HEAVY, Handler
from invoicebot.rules.messages_es import KIND_SUBJECT, MESSAGES, tipo_phrase
from invoicebot.states import CANCELLED, EXTRACT_FAILED, EXTRACTING, RECEIVED, ROUTING, StateMoved
from invoicebot.store.db import Pool
from invoicebot.store.invoices import InvoiceHead, get_file_path, load_invoice_head, save_reading, set_state
from invoicebot.store.padron import lookup

#: BR-E07 counts megapixels; OpenCV's decode cap counts pixels.
PIXELS_PER_MEGAPIXEL = 1_000_000
#: The helper prints one small JSON object; anything larger is a bug the queue retries (R-9.2).
MAX_HELPER_OUTPUT_BYTES = 64 * 1024
logger = logging.getLogger(__name__)

type RuleId = Literal["BR-E01", "BR-E02", "BR-E05"]

#: Job step that routes a read invoice (S4.1).
ROUTE_STEP = "route"
#: Attachment kinds the helper reads, photo first when an invoice somehow has both (R-2.15).
READABLE_KINDS: tuple[AttachmentKind, ...] = ("photo", "pdf")
#: The helper's error when a PDF is over ``limits.media.max_pdf_pages`` (BR-E07, R-2.15).
TOO_MANY_PAGES: Final = "too_many_pages"
#: States in which the handler acts; any other state is left untouched (R-9.4).
ACTIVE_STATES = frozenset({RECEIVED, EXTRACTING})
#: No QR, an unreadable photo or a required field missing (BR-E01).
RULE_UNREADABLE: RuleId = "BR-E01"
#: The gap a repeating unreadable invoice from one issuer raises (BR-E06).
PARSER_MISS = "PARSER_MISS"
#: A RUC is the 11-digit document number (R-11.12).
_RUC_DIGITS = re.compile(r"[0-9]{11}")
#: State each failure rule leaves the invoice in (spec 06).
FAILURE_STATE: dict[RuleId, str] = {
    RULE_UNREADABLE: EXTRACT_FAILED,
    RULE_BAD_RUC: EXTRACT_FAILED,
    RULE_UNSUPPORTED_TIPO: CANCELLED,
}


@dataclass(frozen=True)
class _Failure:
    """The rule an invoice failed with and the partial reading that shows why (BR-E01, BR-E06)."""

    rule_id: RuleId
    reading: Reading | None


class _HelperResult(BaseModel):
    """The one JSON object :mod:`invoicebot.extract` writes to stdout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reading: dict[str, Any] | None
    variant: QrVariant | None
    decoder: QrDecoder | None
    ocr: bool
    error: Literal["unreadable", "too_many_pages"] | None


def make_extract_handler(
    pool: Pool,
    extraction_cfg: Extraction,
    media: MediaLimits,
    rules_path: Path,
    gaps: GapContext,
    helper_prefix: Sequence[str],
) -> Handler:
    """Build the handler that reads an invoice's photo or PDF and queues its routing (S3.11).

    ``rules_path`` is the parsing-rule file the helper reads the OCR fields with, and
    ``gaps`` is who hears about an issuer whose invoices keep being unreadable (BR-E06).
    ``helper_prefix`` is prepended to the helper's argv; empty outside the image (D147).
    The handler acts only on an invoice in ``RECEIVED`` or ``EXTRACTING``. An invoice with no
    photo or PDF file ends with BR-E01 and a message asking for a photo or typed data. A PDF
    over the page cap ends ``CANCELLED`` with BR-E07 ``too_many_pages`` through
    :func:`reject_media` (R-2.15). A
    photo or PDF it cannot read, a required field it does not find, a bad issuer RUC and an
    unsupported document type end the invoice with BR-E01, BR-E02 or BR-E05 and a message to
    the sender. A BR-E01 reading that still shows the issuer's 11-digit RUC counts one
    PARSER_MISS sighting for that issuer. A reading that passes them is stored and the invoice
    moves to ``ROUTING`` with a ``route`` job. A helper that exits non-zero for any other
    reason raises, so the queue retries and, after its limit, fails the invoice with BR-S03.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state not in ACTIVE_STATES:
                return
        file_to_read = await _file_or_reject(pool, job.invoice_id, invoice, gaps, started)
        if file_to_read is None:
            return
        path, kind = file_to_read

        async with pool.connection() as conn, conn.transaction():
            try:
                await set_state(conn, job.invoice_id, EXTRACTING, expected=invoice.state)
            except StateMoved:
                # D95: a /cancelar between the read above and here wins; do not run the helper.
                return

        try:
            result = await _run_helper(path, kind, extraction_cfg, media, rules_path, helper_prefix)
        except TimeoutError:
            await _finish(
                pool,
                job.invoice_id,
                invoice,
                _Failure(RULE_UNREADABLE, None),
                gaps,
                expected=EXTRACTING,
            )
            _log_outcome(job.invoice_id, None, RULE_UNREADABLE, started)
            return

        if result.error == TOO_MANY_PAGES:
            await reject_media(
                pool,
                MediaRejection(
                    job.invoice_id,
                    Addressee(invoice.origin_channel, invoice.profile_id),
                    kind,
                    TOO_MANY_PAGES,
                    media,
                    expected=EXTRACTING,
                ),
            )
            _log_outcome(job.invoice_id, result, RULE_MEDIA_REJECTED, started)
            return

        rule_id, reading = _classify(result, extraction_cfg.supported_tipos)
        if rule_id is not None:
            await _finish(
                pool, job.invoice_id, invoice, _Failure(rule_id, reading), gaps, expected=EXTRACTING
            )
            _log_outcome(job.invoice_id, result, rule_id, started)
            return
        if reading is None:
            raise RuntimeError("extract helper returned neither a reading nor a rule")

        async with pool.connection() as conn, conn.transaction():
            reading = await _with_padron(conn, reading)
            await save_reading(conn, job.invoice_id, reading)
            await set_state(conn, job.invoice_id, ROUTING, expected=EXTRACTING)
            await enqueue(conn, job.invoice_id, ROUTE_STEP)
        _log_outcome(job.invoice_id, result, None, started)

    return handle


async def _file_or_reject(
    pool: Pool,
    invoice_id: int,
    invoice: InvoiceHead,
    gaps: GapContext,
    started: float,
) -> tuple[str, AttachmentKind] | None:
    """Return the photo or PDF to read, or None when the invoice is ended here (R-2.15).

    An invoice with no photo or PDF file ends with BR-E01 and a message asking for a photo or
    typed data. The download step already refused a file over its byte cap (R-1.11).
    """
    async with pool.connection() as conn:
        file_to_read = await _file_to_read(conn, invoice_id)
    if file_to_read is None:
        await _finish(
            pool, invoice_id, invoice, _Failure(RULE_UNREADABLE, None), gaps, expected=invoice.state
        )
        _log_outcome(invoice_id, None, RULE_UNREADABLE, started)
        return None
    return file_to_read


async def _file_to_read(conn: AsyncConnection[Any], invoice_id: int) -> tuple[str, AttachmentKind] | None:
    """Return the invoice's photo or PDF path and its kind, or None when it has neither (R-2.15)."""
    for kind in READABLE_KINDS:
        if (path := await get_file_path(conn, invoice_id, kind)) is not None:
            return path, kind
    return None


async def _run_helper(
    path: str,
    kind: AttachmentKind,
    extraction_cfg: Extraction,
    media: MediaLimits,
    rules_path: Path,
    helper_prefix: Sequence[str],
) -> _HelperResult:
    """Run the extract helper under the heavy slot and return its validated result.

    ``helper_prefix`` is prepended to the helper's argv (D147). The helper receives the input
    kind, that kind's byte cap, the PDF caps, the OCR config and ``rules_path`` as arguments,
    and the file's bytes on stdin — never its path (D158). It runs with a minimal environment
    and an import path that does not include the working directory, and its whole process group
    is killed and reaped on every exit — timeout, crash or cancellation — so no decode process
    or child it started outlives its job (R-9.2).

    Raises:
        TimeoutError: when the helper outlives ``helper_timeout_s``; it is killed and
            reaped first.
        RuntimeError: when the input file cannot be read or is over the media cap, the
            helper exits non-zero, prints more than ``MAX_HELPER_OUTPUT_BYTES`` or prints
            malformed JSON.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Cap the pixels OpenCV may decode, so a decompression bomb is refused early (R-1.11).
        "OPENCV_IO_MAX_IMAGE_PIXELS": str(media.max_megapixels * PIXELS_PER_MEGAPIXEL),
        # One Tesseract thread per job on the 2 GB host.
        "OMP_THREAD_LIMIT": "1",
    }
    data = await asyncio.to_thread(_read_capped, Path(path), max_bytes_for(kind, media))
    argv = [*helper_prefix, *helper_argv(kind, extraction_cfg, media, rules_path)]
    async with HEAVY:
        stdout = await run_helper(argv, data, env, extraction_cfg.helper_timeout_s, MAX_HELPER_OUTPUT_BYTES)
    return _parse_result(stdout)


def _read_capped(path: Path, max_bytes: int) -> bytes:
    """Read the extract input, refusing it unread when it is over the media cap (R-1.11).

    Raises:
        RuntimeError: when the file cannot be read or is larger than ``max_bytes``.
    """
    try:
        if path.stat().st_size > max_bytes:
            raise RuntimeError("extract input exceeds the media cap")
        return path.read_bytes()
    except OSError as err:
        raise RuntimeError("extract input cannot be read") from err


def helper_argv(
    kind: AttachmentKind, extraction_cfg: Extraction, media: MediaLimits, rules_path: Path
) -> list[str]:
    """Return the extract helper's argv for one input kind (D78, D122, D158).

    The input itself travels on the helper's stdin, never as an argument (D158). The golden-set
    test and the accuracy report run the helper with this same argv, so they measure what the
    pipeline reads (R-2.9).
    """
    return [
        sys.executable,
        "-P",
        "-m",
        "invoicebot.extract",
        str(extraction_cfg.max_image_px),
        str(max_bytes_for(kind, media)),
        extraction_cfg.ocr.engine,
        extraction_cfg.ocr.lang,
        str(extraction_cfg.ocr.psm),
        str(extraction_cfg.ocr.timeout_s),
        str(rules_path),
        kind,
        str(media.max_pdf_pages),
        str(extraction_cfg.pdf_min_text_chars),
    ]


def _parse_result(stdout: bytes) -> _HelperResult:
    """Validate the helper's stdout; malformed output is a bug the queue retries (R-9.16)."""
    try:
        return _HelperResult.model_validate_json(stdout)
    except ValidationError:
        # The pydantic error quotes the input, which may be a RUC or an amount (R-9.16).
        raise RuntimeError("extract helper printed malformed JSON") from None


def _classify(result: _HelperResult, supported: Sequence[str]) -> tuple[RuleId | None, Reading | None]:
    """Return the rule the reading breaks and the reading itself (R-2.13, BR-E02, BR-E05)."""
    if result.error is not None or result.reading is None:
        return RULE_UNREADABLE, None
    try:
        reading = reading_from_json(result.reading)
    except ValueError:
        # The helper's payload quotes values such as a RUC or an amount (R-9.16).
        raise RuntimeError("extract helper printed malformed JSON") from None
    if reading.missing_required():
        # Keep the partial reading so BR-E06 can still see an issuer RUC it did read.
        return RULE_UNREADABLE, reading
    issuer = reading.fields["ruc_emisor"].value
    if (rule_id := check_issuer(issuer)) is not None:
        return rule_id, reading
    tipo = reading.fields["tipo"].value
    if (tipo_rule := check_tipo(tipo, supported)) is not None:
        return tipo_rule, reading
    return None, reading


async def _with_padron(conn: AsyncConnection[Any], reading: Reading) -> Reading:
    """Return ``reading`` with its issuer's razón social from the padrón when it has one (R-2.5).

    The lookup runs on the caller's connection, in the transaction that saves the reading,
    so no extra connection is taken and the name cannot outlive a rolled-back save.
    """
    ruc = reading.value("ruc_emisor")
    if ruc is None:
        return reading
    row = await lookup(conn, ruc)
    return with_padron(reading, None if row is None else row.razon_social)


async def _finish(
    pool: Pool,
    invoice_id: int,
    invoice: InvoiceHead,
    failure: _Failure,
    gaps: GapContext,
    *,
    expected: str,
) -> None:
    """Record a failure rule and tell the sender why, in one transaction (BR-E01).

    The write only lands while the invoice is still in ``expected``: an invoice moved on
    by ``/cancelar`` or an edit wins, ``set_state`` raises :class:`StateMoved` and the
    transaction rolls back, so no message and no BR-E06 gap is stored (D95). A BR-E01
    reading that still carries the issuer's RUC counts one PARSER_MISS sighting there.
    """
    rule_id = failure.rule_id
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, FAILURE_STATE[rule_id], rule_id=rule_id, expected=expected)
        await queue_message(
            conn,
            Addressee(invoice.origin_channel, invoice.profile_id),
            invoice_id=invoice_id,
            rule_id=rule_id,
            text=_message(rule_id, failure.reading),
        )
        if (
            rule_id == RULE_UNREADABLE
            and (sighting := _parser_miss_sighting(invoice_id, failure.reading)) is not None
        ):
            await raise_gap(conn, gaps, sighting)


def _parser_miss_sighting(invoice_id: int, reading: Reading | None) -> GapSighting | None:
    """Build one PARSER_MISS sighting from the reading's issuer RUC (BR-E06, R-11.12).

    Returns ``None`` when the reading is missing or its issuer RUC is not exactly 11
    digits, so an unreadable photo that shows no usable issuer raises nothing.
    """
    if reading is None:
        return None
    ruc_emisor = reading.value("ruc_emisor")
    if ruc_emisor is None or _RUC_DIGITS.fullmatch(ruc_emisor) is None:
        return None
    return GapSighting(
        kind=PARSER_MISS,
        parts=(ruc_emisor,),
        subject=KIND_SUBJECT[PARSER_MISS].format(ruc_emisor=ruc_emisor),
        invoice_id=invoice_id,
        fields=IssueFields(
            kind=PARSER_MISS,
            fingerprint=fingerprint(PARSER_MISS, ruc_emisor),
            ruc_emisor=int(ruc_emisor),
            razon_social=reading.value("razon_social"),
            tipo=reading.value("tipo"),
            reasons=(),
            portal_key=None,
            hint_url=None,
        ),
    )


def _message(rule_id: RuleId, reading: Reading | None) -> str:
    """Return the rule's es-PE message, filling the value BR-E02 and BR-E05 name (spec 06)."""
    if rule_id == RULE_BAD_RUC:
        return MESSAGES[rule_id].format(ruc="" if reading is None else reading.value("ruc_emisor") or "")
    if rule_id == RULE_UNSUPPORTED_TIPO:
        tipo = "" if reading is None else reading.value("tipo") or ""
        return MESSAGES[rule_id].format(tipo_desc=tipo_phrase(tipo))
    return MESSAGES[rule_id]


def _log_outcome(
    invoice_id: int,
    result: _HelperResult | None,
    rule_id: str | None,
    started: float,
) -> None:
    """Log ids, the decoder, whether OCR ran and the timing; never an extracted value (R-9.16)."""
    logger.info(
        "step done",
        extra={
            "step": EXTRACT_STEP,
            "invoice_id": invoice_id,
            "variant": None if result is None else result.variant,
            "decoder": None if result is None else result.decoder,
            "ocr": None if result is None else result.ocr,
            "rule_id": rule_id,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
    )
