#!/usr/bin/env python3
"""Report the pipeline's peak memory and latency over synthetic invoices (R-9.1, R-9.2, D160).

Usage: ``memprofile.py [--invoices N] [--timeout-s S]``. The script builds the real pipeline
in-process: the WhatsApp webhook route over a real Postgres database, the real extract, route
and validate handlers, and the real job worker. Two edge adapters are faked, because they need
a live channel: a :class:`FakeChannel` serves the synthetic photo files in place of the WhatsApp
media download, and a no-op fetch handler ends the chain once the summary is queued.

Every photo is generated in-process with a SUNAT QR code for a synthetic RUC from
``tests/synthetic_rucs.txt``; no sample, real RUC, phone number or secret is used. The
webhook secret is a throwaway value generated in-process (``secrets.token_hex``), never read
from the environment or the secret store.

The script prints one JSON object with ``peak_mb`` (own max RSS plus the largest helper's),
``ack_s_max``, ``summary_s_max`` and ``invoices``. It exits 1 when the peak exceeds 800 MB,
an acknowledgement takes 5 s or more, a summary takes 60 s or more, or an invoice ends with
no summary. When ``GITHUB_STEP_SUMMARY`` is set it appends a Markdown table there.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import resource
import secrets
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import cv2
import httpx
import numpy as np
from fastapi import FastAPI
from psycopg import AsyncConnection

from invoicebot.api.intake_route import IntakeDeps
from invoicebot.api.webhook import WebhookDeps, make_router
from invoicebot.channels.base import Attachment, InboundMessage
from invoicebot.channels.fake import FakeChannel
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, RoutingRule, StoreDirectory
from invoicebot.pipeline.extract import ROUTE_STEP, make_extract_handler
from invoicebot.pipeline.fetch import FETCH_STEP
from invoicebot.pipeline.gaps import GapContext
from invoicebot.pipeline.handlers import EXTRACT_STEP
from invoicebot.pipeline.intake import DOWNLOAD_STEP, RULE_ACK
from invoicebot.pipeline.intake_config import intake_settings
from invoicebot.pipeline.route import make_route_handler
from invoicebot.pipeline.validate import VALIDATE_STEP, make_validate_handler
from invoicebot.queue.jobs import Job, enqueue
from invoicebot.queue.worker import Handler, Worker
from invoicebot.routing.router import compile_rules
from invoicebot.settings import Settings
from invoicebot.store.db import Pool, create_pool
from invoicebot.store.invoices import FileRecord, add_file, has_file

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "config" / "example.yaml"
PARSING_RULES = REPO_ROOT / "config" / "parsing_rules.yaml"
#: The example config's full profile; the run copies it under a fresh id.
OWNER_PROFILE = "yo"
#: The flow every synthetic invoice routes to (R-3.1).
ROUTED_FLOW = "personal_google"
#: Synthetic values from ``tests/synthetic_rucs.txt`` and ``config/example.yaml`` (R-9.17).
ISSUER_RUC = "20000000001"
BUYER_RUC = "20000000028"
SENDER_NUMBER = "51900000000"
SERIE = "F001"
IGV = "7.61"
TOTAL = "49.90"
#: Throwaway webhook values generated in-process (R-9.9); never environment or store secrets.
APP_SECRET = secrets.token_hex(32)
VERIFY_TOKEN = secrets.token_hex(16)
SENDER_HASH_KEY = secrets.token_hex(32)
#: Limits D160 enforces: 800 MB peak, 5 s per acknowledgement, 60 s per summary (R-9.1, R-9.2).
PEAK_LIMIT_MB = 800.0
ACK_LIMIT_S = 5.0
SUMMARY_LIMIT_S = 60.0
DEFAULT_INVOICES = 10
DEFAULT_TIMEOUT_S = 180.0
#: How often the report checks whether a summary reached the outbox (s).
POLL_INTERVAL_S = 0.2
_KILOBYTES_PER_MEGABYTE = 1024
_WHITE = 255
_CANVAS_PX = 800
_QR_SCALE = 6
_QR_OFFSET_PX = 100
_EMPTY_DIRECTORY = StoreDirectory(stores={}, portals={})


@dataclass(frozen=True)
class _Setup:
    """The config and identities one measurement run uses."""

    cfg: Config
    profile_id: str
    summary_rule: str


@dataclass(frozen=True)
class _Post:
    """One webhook post and the time its acknowledgement took."""

    invoice_id: int
    posted_at: float
    ack_s: float


@dataclass(frozen=True)
class _Report:
    """One measurement run's results (D160)."""

    peak_mb: float
    ack_s_max: float
    summary_s_max: float
    invoices: int
    missing: int

    @property
    def ok(self) -> bool:
        """Return whether every threshold and every summary held (R-9.1, R-9.2)."""
        return (
            self.peak_mb <= PEAK_LIMIT_MB
            and self.ack_s_max < ACK_LIMIT_S
            and self.summary_s_max < SUMMARY_LIMIT_S
            and self.missing == 0
        )


def main(argv: list[str] | None = None) -> int:
    """Run the measurement, print its JSON object and append the summary table (D160)."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.invoices < 1:
        parser.error("--invoices must be at least 1")
    database_url = Settings().database_url
    if database_url is None:
        print("error: DATABASE_URL must be set", file=sys.stderr)
        return 1
    report = asyncio.run(_measure(database_url, args.invoices, args.timeout_s))
    _print_report(report)
    return 0 if report.ok else 1


def _parser() -> argparse.ArgumentParser:
    """Return the parser for the memory and latency report (D160)."""
    parser = argparse.ArgumentParser(description="Report the pipeline's peak memory and latency.")
    parser.add_argument(
        "--invoices",
        type=int,
        default=DEFAULT_INVOICES,
        help=f"synthetic invoices to push through the pipeline (default {DEFAULT_INVOICES})",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"seconds to wait for every summary (default {DEFAULT_TIMEOUT_S:g})",
    )
    return parser


async def _measure(database_url: str, invoices: int, timeout_s: float) -> _Report:
    """Open the pool and run one measurement over a temporary media directory (D160)."""
    setup = _setup()
    pool = create_pool(database_url)
    await pool.open(wait=True)
    try:
        with tempfile.TemporaryDirectory(prefix="memprofile-") as media_dir:
            return await _run(pool, setup, Path(media_dir), invoices, timeout_s)
    finally:
        await pool.close()


def _setup() -> _Setup:
    """Load the example config and give the run its own profile and routing rule (R-3.1)."""
    base = load_config(EXAMPLE_CONFIG)
    rule = _rule_for_flow(base)
    routing = base.routing.model_copy(
        update={"rules": [rule.model_copy(update={"when": {"num_doc_adq": [BUYER_RUC]}})]}
    )
    profile_id = f"memprofile-{uuid4().hex}"
    profile = base.profiles[OWNER_PROFILE].model_copy(update={"whatsapp": SENDER_NUMBER})
    cfg = base.model_copy(update={"profiles": {profile_id: profile}, "routing": routing})
    return _Setup(cfg=cfg, profile_id=profile_id, summary_rule=rule.id)


def _rule_for_flow(cfg: Config) -> RoutingRule:
    """Return the example config's rule for :data:`ROUTED_FLOW`, whose id the summary carries."""
    for rule in cfg.routing.rules:
        if rule.flow == ROUTED_FLOW:
            return rule
    raise RuntimeError(f"the example config has no routing rule for {ROUTED_FLOW}")


async def _run(pool: Pool, setup: _Setup, media_dir: Path, invoices: int, timeout_s: float) -> _Report:
    """Post ``invoices`` synthetic photos, then run the worker until their summaries arrive."""
    media = FakeChannel()
    app = _app(pool, setup)
    transport = httpx.ASGITransport(app=app)
    # A SUNAT QR numero has at most eight digits (extract/qr_payload.py).
    base_numero = uuid4().int % 90_000_000 + 1_000_000
    posts: list[_Post] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for index in range(invoices):
            posts.append(await _post_invoice(client, pool, setup, media, media_dir, base_numero + index))
    return await _run_worker(pool, setup, media, posts, timeout_s)


def _app(pool: Pool, setup: _Setup) -> FastAPI:
    """Build the app with the WhatsApp webhook route over the measurement pool (R-1.1)."""
    app = FastAPI()
    app.include_router(make_router(_deps(pool, setup)))
    return app


def _deps(pool: Pool, setup: _Setup) -> WebhookDeps:
    """Build the webhook dependencies for the run's single allowlisted profile (R-1.9)."""
    profile = setup.cfg.profiles[setup.profile_id]
    return WebhookDeps(
        intake_deps=IntakeDeps(pool=pool, intake=intake_settings(setup.cfg)),
        index=ProfileIndex({setup.profile_id: profile}),
        owner_profile=setup.profile_id,
        app_secret=APP_SECRET,
        verify_token=VERIFY_TOKEN,
        sender_hash_key=SENDER_HASH_KEY,
    )


async def _post_invoice(
    client: httpx.AsyncClient,
    pool: Pool,
    setup: _Setup,
    media: FakeChannel,
    media_dir: Path,
    numero: int,
) -> _Post:
    """Generate one QR photo, post it to the webhook and time its acknowledgement (R-1.1)."""
    today = datetime.now(ZoneInfo(setup.cfg.owner.timezone)).date()
    media_ref = f"media-{uuid4().hex}"
    media.add_media(media_ref, _write_photo(media_dir / f"{media_ref}.png", _qr_payload(numero, today)))
    body = _webhook_body(media_ref)
    acks_before = await _ack_count(pool, setup.profile_id)
    started = time.perf_counter()
    response = await client.post("/webhook/whatsapp", content=body, headers=_signature(body))
    ack_s = time.perf_counter() - started
    if response.status_code != 200:
        raise RuntimeError(f"the webhook answered {response.status_code}")
    if await _ack_count(pool, setup.profile_id) != acks_before + 1:
        raise RuntimeError("the webhook stored no acknowledgement for the posted photo")
    invoice_id = await _newest_invoice(pool, setup.profile_id)
    return _Post(invoice_id=invoice_id, posted_at=started, ack_s=ack_s)


def _qr_payload(numero: int, today: date) -> str:
    """Return a SUNAT QR payload that routes from the synthetic issuer to the synthetic buyer."""
    return f"{ISSUER_RUC}|01|{SERIE}|{numero}|{IGV}|{TOTAL}|{today.isoformat()}|6|{BUYER_RUC}"


def _write_photo(path: Path, payload: str) -> Path:
    """Write a white PNG carrying ``payload`` as a QR code, as the conversation fixtures do."""
    canvas = np.full((_CANVAS_PX, _CANVAS_PX), _WHITE, dtype=np.uint8)
    code = cv2.QRCodeEncoder.create().encode(payload)
    code = cv2.resize(code, None, fx=_QR_SCALE, fy=_QR_SCALE, interpolation=cv2.INTER_NEAREST)
    height, width = code.shape
    canvas[_QR_OFFSET_PX : _QR_OFFSET_PX + height, _QR_OFFSET_PX : _QR_OFFSET_PX + width] = code
    ok, encoded = cv2.imencode(".png", canvas)
    if not ok:
        raise RuntimeError("could not encode the synthetic photo")
    path.write_bytes(encoded.tobytes())
    return path


def _webhook_body(media_ref: str) -> bytes:
    """Return the signed body of a Meta photo webhook carrying ``media_ref`` (R-1.1)."""
    message = {
        "from": SENDER_NUMBER,
        "id": f"wamid.{uuid4().hex}",
        "timestamp": str(int(datetime.now(UTC).timestamp())),
        "type": "image",
        "image": {"id": media_ref, "mime_type": "image/png"},
    }
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {"messaging_product": "whatsapp", "messages": [message]},
                    }
                ],
            }
        ],
    }
    return json.dumps(payload).encode("utf-8")


def _signature(body: bytes) -> dict[str, str]:
    """Return the X-Hub-Signature-256 header Meta would send for ``body`` (R-1.3)."""
    digest = hmac.new(APP_SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": f"sha256={digest}"}


async def _newest_invoice(pool: Pool, profile_id: str) -> int:
    """Return the invoice the webhook just created for the run's profile."""
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT max(id) FROM invoices WHERE profile_id = %s", (profile_id,))
        row = await cur.fetchone()
    if row is None or row[0] is None:
        raise RuntimeError(f"the webhook stored no invoice for {profile_id}")
    return int(row[0])


async def _ack_count(pool: Pool, profile_id: str) -> int:
    """Return how many BR-S09 acknowledgements the profile's outbox holds (R-1.1)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """SELECT count(*) FROM outbox
               WHERE "to" = %s AND payload_json->>'rule_id' = %s""",
            (profile_id, RULE_ACK),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError("the database returned no acknowledgement count")
    return int(row[0])


async def _no_dead_notice(_conn: AsyncConnection[Any], _invoice_id: int, _step: str, _error: str) -> None:
    """Queue no BR-S03 notice: the profile measures invoices that succeed (D160, D190)."""


async def _run_worker(
    pool: Pool, setup: _Setup, media: FakeChannel, posts: list[_Post], timeout_s: float
) -> _Report:
    """Run the real worker and record when each invoice's summary reaches the outbox (D160)."""
    async with httpx.AsyncClient() as http:
        worker = Worker(pool, _handlers(pool, setup, media, http), on_dead=_no_dead_notice)
        task = asyncio.create_task(worker.run())
        try:
            found = await _wait_for_summaries(pool, setup.summary_rule, posts, timeout_s)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    return _report(posts, found)


def _handlers(pool: Pool, setup: _Setup, media: FakeChannel, http: httpx.AsyncClient) -> dict[str, Handler]:
    """Return the pipeline's handlers, faking only the WhatsApp edge of the chain (D160)."""
    cfg = setup.cfg
    return {
        DOWNLOAD_STEP: _fake_download(pool, media),
        EXTRACT_STEP: make_extract_handler(
            pool,
            cfg.extraction,
            cfg.limits.media,
            PARSING_RULES,
            GapContext(profiles=cfg.profiles, gaps_cfg=cfg.gaps),
            helper_prefix=(),
        ),
        ROUTE_STEP: make_route_handler(pool, cfg, _EMPTY_DIRECTORY, compile_rules(cfg.routing)),
        VALIDATE_STEP: make_validate_handler(pool, http, None, cfg),
        FETCH_STEP: _skip_fetch,
    }


def _fake_download(pool: Pool, media: FakeChannel) -> Handler:
    """Build the download stand-in: the fake channel serves the photo the webhook announced."""

    async def handle(job: Job) -> None:
        async with pool.connection() as conn:
            if await has_file(conn, job.invoice_id):
                return
        attachment = await _first_attachment(pool, job.invoice_id)
        path = await media.fetch_media(attachment.media_ref)
        data = await asyncio.to_thread(path.read_bytes)
        record = FileRecord(
            kind=attachment.kind,
            local_path=str(path),
            remote_ref=attachment.media_ref,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
        )
        async with pool.connection() as conn, conn.transaction():
            await add_file(conn, job.invoice_id, record)
            await enqueue(conn, job.invoice_id, EXTRACT_STEP)

    return handle


async def _first_attachment(pool: Pool, invoice_id: int) -> Attachment:
    """Return the first attachment of the invoice's oldest message (R-1.4)."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT payload_json FROM messages
            WHERE invoice_id = %s
            ORDER BY received_at, external_id
            LIMIT 1
            """,
            (invoice_id,),
        )
        row = await cur.fetchone()
    if row is None:
        raise RuntimeError(f"invoice {invoice_id} has no message")
    message = InboundMessage.model_validate(row[0])
    if not message.attachments:
        raise RuntimeError(f"invoice {invoice_id}'s message has no attachment")
    return message.attachments[0]


async def _skip_fetch(_job: Job) -> None:
    """Complete the fetch job without fetching: this run measures up to the summary (D160)."""


async def _wait_for_summaries(
    pool: Pool, rule_id: str, posts: list[_Post], timeout_s: float
) -> dict[int, float]:
    """Wait until every posted invoice has a summary outbox row, or the budget runs out."""
    pending = {post.invoice_id: post.posted_at for post in posts}
    found: dict[int, float] = {}
    deadline = time.perf_counter() + timeout_s
    while pending and time.perf_counter() < deadline:
        for invoice_id in await _summaries_in_outbox(pool, rule_id, list(pending)):
            found[invoice_id] = time.perf_counter() - pending.pop(invoice_id)
        if pending:
            await asyncio.sleep(POLL_INTERVAL_S)
    return found


async def _summaries_in_outbox(pool: Pool, rule_id: str, invoice_ids: list[int]) -> list[int]:
    """Return which of ``invoice_ids`` already have their summary outbox row."""
    async with pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT invoice_id FROM outbox
            WHERE invoice_id = ANY(%s) AND payload_json->>'rule_id' = %s
            """,
            (invoice_ids, rule_id),
        )
        rows = await cur.fetchall()
    return [int(row[0]) for row in rows]


def _report(posts: list[_Post], found: dict[int, float]) -> _Report:
    """Collect the maxima and the missing count from the run (R-9.1, R-9.2)."""
    return _Report(
        peak_mb=_peak_mb(),
        ack_s_max=max(post.ack_s for post in posts),
        summary_s_max=max(found.values(), default=0.0),
        invoices=len(posts),
        missing=len(posts) - len(found),
    )


def _peak_mb() -> float:
    """Return own max RSS plus the largest helper's, in megabytes (R-9.1, D160)."""
    own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return round((own + children) / _KILOBYTES_PER_MEGABYTE, 1)


def _print_report(report: _Report) -> None:
    """Print the one JSON object and, when GitHub asks, append the Markdown table (D160)."""
    payload = {
        "peak_mb": report.peak_mb,
        "ack_s_max": round(report.ack_s_max, 2),
        "summary_s_max": round(report.summary_s_max, 2),
        "invoices": report.invoices,
    }
    print(json.dumps(payload))
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(_markdown(report))


def _markdown(report: _Report) -> str:
    """Return the Markdown table GitHub shows for this run (D160)."""
    result = "pass" if report.ok else "fail"
    return (
        "\n| Metric | Value |\n"
        "| --- | --- |\n"
        f"| Peak memory (MB, limit {PEAK_LIMIT_MB:g}) | {report.peak_mb} |\n"
        f"| Ack max (s, limit {ACK_LIMIT_S:g}) | {report.ack_s_max:.2f} |\n"
        f"| Summary max (s, limit {SUMMARY_LIMIT_S:g}) | {report.summary_s_max:.2f} |\n"
        f"| Invoices | {report.invoices} |\n"
        f"| Missing summaries | {report.missing} |\n"
        f"| Result | {result} |\n"
    )


if __name__ == "__main__":
    raise SystemExit(main())
