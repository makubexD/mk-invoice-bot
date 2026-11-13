"""Job handler that reads one invoice photo in a helper process (S3.11, R-2.1).

:func:`make_extract_handler` builds the handler of the ``extract`` job step. QR decoding and
OCR run in ``python -m invoicebot.extract`` under the heavy-work slot, so the bot process
never loads OpenCV, zxing or Tesseract and their memory returns when the helper exits (D78).
The helper's one JSON line is validated before use. The outcome's state change, next job and
outbox message commit in one transaction (CLAUDE.md "Database").
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from invoicebot.channels.base import AttachmentKind, ChannelName, OutboundMessage
from invoicebot.config.models import Extraction, MediaLimits
from invoicebot.extract.fields import Reading, reading_from_json
from invoicebot.extract.validate import RULE_BAD_RUC, RULE_UNSUPPORTED_TIPO, check_issuer, check_tipo
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import HEAVY, Handler
from invoicebot.rules.messages_es import MESSAGES, tipo_phrase
from invoicebot.store.db import Pool
from invoicebot.store.invoices import get_file_path, load_invoice_head, save_reading, set_state

#: BR-E07 counts megapixels; OpenCV's decode cap counts pixels.
PIXELS_PER_MEGAPIXEL = 1_000_000
#: The helper prints one small JSON object; anything larger is a bug the queue retries (R-9.2).
MAX_HELPER_OUTPUT_BYTES = 64 * 1024
logger = logging.getLogger(__name__)

type RuleId = Literal["BR-E01", "BR-E02", "BR-E05"]

#: Job step that routes a read invoice (S4.1).
ROUTE_STEP = "route"
#: Attachment kind the helper reads.
PHOTO_KIND: AttachmentKind = "photo"
#: States in which the handler acts; any other state is left untouched (R-9.4).
ACTIVE_STATES = frozenset({"RECEIVED", "EXTRACTING"})
#: The state of an invoice the pipeline stops processing.
CANCELLED_STATE = "CANCELLED"
#: No QR, an unreadable photo or a required field missing (BR-E01).
RULE_UNREADABLE: RuleId = "BR-E01"
#: State each failure rule leaves the invoice in (spec 06).
FAILURE_STATE: dict[RuleId, str] = {
    RULE_UNREADABLE: "EXTRACT_FAILED",
    RULE_BAD_RUC: "EXTRACT_FAILED",
    RULE_UNSUPPORTED_TIPO: CANCELLED_STATE,
}


class _HelperResult(BaseModel):
    """The one JSON object :mod:`invoicebot.extract` writes to stdout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reading: dict[str, Any] | None
    variant: str | None
    decoder: str | None
    ocr: bool
    error: Literal["unreadable"] | None


def make_extract_handler(
    pool: Pool, extraction_cfg: Extraction, media: MediaLimits, rules_path: Path
) -> Handler:
    """Build the handler that reads an invoice's photo and queues its routing (S3.11).

    ``rules_path`` is the parsing-rule file the helper reads the OCR fields with. The handler
    acts only on an invoice in ``RECEIVED`` or ``EXTRACTING``. An invoice with no photo file —
    a PDF or XML attachment until read-more ships — ends with BR-E01 and a message asking for a
    photo or typed data. A photo it cannot read, a required field it does not find, a bad
    issuer RUC and an unsupported document type end the invoice with BR-E01, BR-E02 or BR-E05
    and a message to the sender. A reading that passes them is stored and the invoice moves to
    ``ROUTING`` with a ``route`` job. A helper that exits non-zero for any other reason raises,
    so the queue retries and, after its limit, fails the invoice with BR-S03.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await load_invoice_head(conn, job.invoice_id)
            if invoice is None or invoice.state not in ACTIVE_STATES:
                return
            path = await get_file_path(conn, job.invoice_id, PHOTO_KIND)
            profile_id = invoice.profile_id
            channel = invoice.origin_channel
        if path is None:
            # shortcut: PDF and XML input arrive in read-more; until then a non-photo
            # file asks for a photo or typed data (BR-E01).
            await _finish(pool, job.invoice_id, channel, profile_id, RULE_UNREADABLE, None)
            _log_outcome(job.invoice_id, None, RULE_UNREADABLE, started)
            return

        async with pool.connection() as conn, conn.transaction():
            await set_state(conn, job.invoice_id, "EXTRACTING")

        try:
            result = await _run_helper(path, extraction_cfg, media, rules_path)
        except TimeoutError:
            await _finish(pool, job.invoice_id, channel, profile_id, RULE_UNREADABLE, None)
            _log_outcome(job.invoice_id, None, RULE_UNREADABLE, started)
            return

        rule_id, reading = _classify(result, extraction_cfg.supported_tipos)
        if rule_id is not None:
            await _finish(pool, job.invoice_id, channel, profile_id, rule_id, reading)
            _log_outcome(job.invoice_id, result, rule_id, started)
            return
        if reading is None:
            raise RuntimeError("extract helper returned neither a reading nor a rule")

        async with pool.connection() as conn, conn.transaction():
            await save_reading(conn, job.invoice_id, reading)
            await set_state(conn, job.invoice_id, "ROUTING")
            await enqueue(conn, job.invoice_id, ROUTE_STEP)
        _log_outcome(job.invoice_id, result, None, started)

    return handle


async def _run_helper(
    path: str, extraction_cfg: Extraction, media: MediaLimits, rules_path: Path
) -> _HelperResult:
    """Run the extract helper under the heavy slot and return its validated result.

    The helper receives the photo caps, the OCR config and ``rules_path`` as arguments. It
    runs with a minimal environment and an import path that does not include the working
    directory, and its whole process group is killed and reaped on every exit — timeout,
    crash or cancellation — so no decode process or child it started outlives its job
    (R-9.2).

    Raises:
        TimeoutError: when the helper outlives ``helper_timeout_s``; it is killed and
            reaped first.
        RuntimeError: when the helper exits non-zero, prints more than
            ``MAX_HELPER_OUTPUT_BYTES`` or prints malformed JSON.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Cap the pixels OpenCV may decode, so a decompression bomb is refused early (R-1.11).
        "OPENCV_IO_MAX_IMAGE_PIXELS": str(media.max_megapixels * PIXELS_PER_MEGAPIXEL),
        # One Tesseract thread per job on the 2 GB host.
        "OMP_THREAD_LIMIT": "1",
    }
    async with HEAVY:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-P",
            "-m",
            "invoicebot.extract",
            path,
            str(extraction_cfg.max_image_px),
            str(media.max_bytes.image),
            extraction_cfg.ocr.engine,
            extraction_cfg.ocr.lang,
            str(extraction_cfg.ocr.psm),
            str(extraction_cfg.ocr.timeout_s),
            str(rules_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(extraction_cfg.helper_timeout_s):
                stdout_stream = cast(asyncio.StreamReader, process.stdout)
                chunks: list[bytes] = []
                remaining = MAX_HELPER_OUTPUT_BYTES + 1
                while remaining > 0:
                    chunk = await stdout_stream.read(remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                stdout = b"".join(chunks)
                if len(stdout) > MAX_HELPER_OUTPUT_BYTES:
                    raise RuntimeError("extract helper output exceeds the cap")
                await process.wait()
        finally:
            # Kill the group in every case, so a child of a helper that already exited dies too.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            if process.returncode is None:
                await process.wait()
    if process.returncode != 0:
        raise RuntimeError(f"extract helper exited with code {process.returncode}")
    return _parse_result(stdout)


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
        return RULE_UNREADABLE, None
    issuer = reading.fields["ruc_emisor"].value
    if (rule_id := check_issuer(issuer)) is not None:
        return rule_id, reading
    tipo = reading.fields["tipo"].value
    if (tipo_rule := check_tipo(tipo, supported)) is not None:
        return tipo_rule, reading
    return None, reading


async def _finish(
    pool: Pool,
    invoice_id: int,
    channel: ChannelName,
    profile_id: str,
    rule_id: RuleId,
    reading: Reading | None,
) -> None:
    """Record a failure rule and tell the sender why, in one transaction (BR-E01)."""
    message = OutboundMessage(
        channel=channel,
        recipient=profile_id,
        profile_id=profile_id,
        rule_id=rule_id,
        text=_message(rule_id, reading),
    )
    async with pool.connection() as conn, conn.transaction():
        await set_state(conn, invoice_id, FAILURE_STATE[rule_id], rule_id=rule_id)
        await outbox.add(conn, channel, profile_id, message.model_dump(mode="json"))


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
    rule_id: RuleId | None,
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
