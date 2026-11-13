"""Job handler that reads one invoice photo in a helper process (S3.11, R-2.1).

:func:`make_extract_handler` builds the handler of the ``extract`` job step. QR decoding
runs in ``python -m invoicebot.extract`` under the heavy-work slot, so the bot process
never loads OpenCV or zxing and their memory returns when the helper exits (D78). The
helper's one JSON line is validated before use. The outcome's state change, next job and
outbox message commit in one transaction (CLAUDE.md "Database").
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections.abc import Sequence
from typing import Any, Literal, cast

from psycopg import AsyncConnection
from pydantic import BaseModel, ConfigDict, ValidationError

from invoicebot.channels.base import AttachmentKind, ChannelName, OutboundMessage
from invoicebot.config.models import Extraction, MediaLimits
from invoicebot.extract.fields import Field, Reading, Source
from invoicebot.extract.validate import check_issuer, check_tipo
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.queue import outbox
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import HEAVY, Handler
from invoicebot.rules.messages_es import MESSAGES, tipo_phrase
from invoicebot.store.db import Pool
from invoicebot.store.invoices import get_file_path, get_state, save_reading, set_state

#: BR-E07 counts megapixels; OpenCV's decode cap counts pixels.
PIXELS_PER_MEGAPIXEL = 1_000_000
logger = logging.getLogger(__name__)

type RuleId = Literal["BR-E01", "BR-E02", "BR-E05"]

#: Job step that routes a read invoice (S4.1).
ROUTE_STEP = "route"
#: Attachment kind the helper reads.
PHOTO_KIND: AttachmentKind = "photo"
#: States in which the handler acts; any other state is left untouched (R-9.4).
ACTIVE_STATES = frozenset({"RECEIVED", "EXTRACTING"})
#: No QR, an unreadable photo or a required field missing (BR-E01).
RULE_UNREADABLE: RuleId = "BR-E01"
#: The issuer RUC fails its check digit (BR-E02).
RULE_BAD_RUC: RuleId = "BR-E02"
#: The document type is not supported (BR-E05).
RULE_UNSUPPORTED_TIPO: RuleId = "BR-E05"
#: State each failure rule leaves the invoice in (spec 06).
FAILURE_STATE: dict[RuleId, str] = {
    RULE_UNREADABLE: "EXTRACT_FAILED",
    RULE_BAD_RUC: "EXTRACT_FAILED",
    RULE_UNSUPPORTED_TIPO: "CANCELLED",
}


class _FieldPayload(BaseModel):
    """One field as the helper prints it (R-2.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: str
    source: Source
    confidence: float


class _ReadingPayload(BaseModel):
    """A reading as the helper prints it (R-2.6, R-2.8)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fields: dict[str, _FieldPayload]
    document_url: str | None

    def to_reading(self) -> Reading:
        """Return the payload as the core reading model."""
        return Reading(
            fields={
                name: Field(value=payload.value, source=payload.source, confidence=payload.confidence)
                for name, payload in self.fields.items()
            },
            document_url=self.document_url,
        )


class _HelperResult(BaseModel):
    """The one JSON object :mod:`invoicebot.extract` writes to stdout."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reading: _ReadingPayload | None
    variant: str | None
    decoder: str | None
    error: Literal["unreadable"] | None


def make_extract_handler(pool: Pool, extraction_cfg: Extraction, media: MediaLimits) -> Handler:
    """Build the handler that reads an invoice's photo and queues its routing (S3.11).

    The handler acts only on an invoice in ``RECEIVED`` or ``EXTRACTING``. An invoice with
    no photo file — a PDF or XML attachment until read-more ships — ends with BR-E01 and a
    message asking for a photo or typed data. A photo it cannot read, a required field it
    does not find, a bad issuer RUC and an unsupported document type end the invoice with
    BR-E01, BR-E02 or BR-E05 and a message to the sender. A reading that passes them is
    stored and the invoice moves to ``ROUTING`` with a ``route`` job. A helper that exits
    non-zero for any other reason raises, so the queue retries and, after its limit, fails
    the invoice with BR-S03.
    """

    async def handle(job: Job) -> None:
        started = time.perf_counter()
        async with pool.connection() as conn:
            invoice = await get_state(conn, job.invoice_id)
            if invoice is None or invoice[0] not in ACTIVE_STATES:
                return
            _, profile_id = invoice
            path = await get_file_path(conn, job.invoice_id, PHOTO_KIND)
            channel = await _origin_channel(conn, job.invoice_id)
        if path is None:
            # shortcut: PDF and XML input arrive in read-more; until then a non-photo
            # file asks for a photo or typed data (BR-E01).
            await _finish(pool, job.invoice_id, channel, profile_id, RULE_UNREADABLE, None)
            _log_outcome(job.invoice_id, None, RULE_UNREADABLE, started)
            return

        async with pool.connection() as conn, conn.transaction():
            await set_state(conn, job.invoice_id, "EXTRACTING")

        try:
            result = await _run_helper(path, extraction_cfg, media)
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


async def _origin_channel(conn: AsyncConnection[Any], invoice_id: int) -> ChannelName:
    """Return the channel the invoice arrived on, which its replies use."""
    cur = await conn.execute("SELECT origin_channel FROM invoices WHERE id = %s", (invoice_id,))
    row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"invoice {invoice_id} disappeared")
    return cast(ChannelName, row[0])


async def _run_helper(path: str, extraction_cfg: Extraction, media: MediaLimits) -> _HelperResult:
    """Run the extract helper under the heavy slot and return its validated result.

    The helper runs with a minimal environment and an import path that does not include
    the working directory, and it is killed and reaped on every exit — timeout, crash or
    cancellation — so no decode process outlives its job (R-9.2).

    Raises:
        TimeoutError: when the helper outlives ``helper_timeout_s``; it is killed and
            reaped first.
        RuntimeError: when the helper exits non-zero or prints malformed JSON.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        # Cap the pixels OpenCV may decode, so a decompression bomb is refused early (R-1.11).
        "OPENCV_IO_MAX_IMAGE_PIXELS": str(media.max_megapixels * PIXELS_PER_MEGAPIXEL),
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
        )
        try:
            async with asyncio.timeout(extraction_cfg.helper_timeout_s):
                stdout, _ = await process.communicate()
        finally:
            if process.returncode is None:
                process.kill()
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
    reading = result.reading.to_reading()
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
        return MESSAGES[rule_id].format(ruc=_field_value(reading, "ruc_emisor"))
    if rule_id == RULE_UNSUPPORTED_TIPO:
        return MESSAGES[rule_id].format(tipo_desc=tipo_phrase(_field_value(reading, "tipo")))
    return MESSAGES[rule_id]


def _field_value(reading: Reading | None, name: str) -> str:
    """Return the reading's value for ``name``, or an empty string when it is absent."""
    field = None if reading is None else reading.fields.get(name)
    return "" if field is None else field.value


def _log_outcome(
    invoice_id: int,
    result: _HelperResult | None,
    rule_id: RuleId | None,
    started: float,
) -> None:
    """Log ids, the decoder that hit and the timing; never an extracted value (R-9.16)."""
    logger.info(
        "step done",
        extra={
            "step": EXTRACT_STEP,
            "invoice_id": invoice_id,
            "variant": None if result is None else result.variant,
            "decoder": None if result is None else result.decoder,
            "rule_id": rule_id,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        },
    )
