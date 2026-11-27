"""Ask the sender for a portal's captcha and resume the fetch with the answer (D209)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from psycopg import AsyncConnection

from invoicebot.channels.base import Document
from invoicebot.channels.media_limits import DIRECTORY_MODE
from invoicebot.config.models import StoreDirectory
from invoicebot.fetchers.base import FetchInvoice, FetchResult
from invoicebot.fetchers.browser.protocol import CaptchaChallenge, CaptchaForm
from invoicebot.pipeline.outgoing import Addressee, queue_document
from invoicebot.pipeline.steps import CAPTCHA_TIMEOUT_STEP
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import MESSAGES
from invoicebot.states import FETCHING_DOCS, WAITING_CAPTCHA
from invoicebot.store import captchas
from invoicebot.store.invoices import set_state

#: The folder under the data directory that holds the captcha images being asked (D209).
CAPTCHA_DIRECTORY = "captchas"
#: The rule of the first ask of a round (BR-F18).
RULE_ASK = "BR-F18"
#: The rule of an ask that follows a refused answer (BR-F20).
RULE_ASK_AGAIN = "BR-F20"
_IMAGE_MIME = "image/png"
_IMAGE_NAME = "captcha.png"
_SECONDS_PER_MINUTE = 60


@runtime_checkable
class CaptchaResumer(Protocol):
    """A fetcher that can continue a fetch the sender's captcha answer unblocks (D209)."""

    async def resume(
        self, invoice: FetchInvoice, form: CaptchaForm, answer: str, out_dir: Path
    ) -> FetchResult:
        """Post ``answer`` for the saved ``form`` and report why the fetch ended, as ``fetch`` does."""
        ...


@dataclass(frozen=True)
class AskContext:
    """What every ask needs besides the invoice: where files live, the stores, the timeout and the sender."""

    data_dir: Path
    directory: StoreDirectory
    timeout_s: float
    to: Addressee


@dataclass(frozen=True)
class AskedInvoice:
    """The invoice a captcha is asked for; ``after_wrong`` says the last answer was refused (BR-F20)."""

    invoice_id: int
    ruc_emisor: str
    after_wrong: bool


async def ask(
    conn: AsyncConnection[Any], ctx: AskContext, invoice: AskedInvoice, result: FetchResult
) -> None:
    """Send the captcha of ``result`` to the sender and park the invoice until the answer (D209).

    In the caller's transaction: the PNG moves under the data directory, the pending
    row is stored, the invoice goes ``FETCHING_DOCS`` to ``WAITING_CAPTCHA``, the image is
    queued with its caption, and the timeout job is enqueued at the row's ``expires_at``.
    A failure inside the ask removes the moved PNG again.
    """
    challenge = result.challenge
    if challenge is None or result.portal_key is None:
        raise RuntimeError(f"invoice {invoice.invoice_id}: a captcha ask needs a challenge and a portal")
    folder = ctx.data_dir / CAPTCHA_DIRECTORY / str(invoice.invoice_id)
    folder.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    image = folder / f"{uuid4().hex}.png"
    await asyncio.to_thread(challenge.path.replace, image)
    # shortcut: a commit that fails after ask returns leaves this PNG (at most 64 KiB) behind.
    try:
        await _store_ask(conn, ctx, invoice, (result.portal_key, challenge), image)
    except BaseException:
        image.unlink(missing_ok=True)
        raise


async def _store_ask(
    conn: AsyncConnection[Any],
    ctx: AskContext,
    invoice: AskedInvoice,
    asked: tuple[str, CaptchaChallenge],
    image: Path,
) -> None:
    """Write the ask's rows for the PNG already at ``image``; the caller owns the transaction (D209)."""
    portal_key, challenge = asked
    rule_id = RULE_ASK_AGAIN if invoice.after_wrong else RULE_ASK
    await captchas.put(
        conn,
        invoice.invoice_id,
        portal_key,
        challenge.form.model_dump(mode="json"),
        str(image),
        ctx.timeout_s,
    )
    pending = await captchas.get(conn, invoice.invoice_id)
    if pending is None:
        raise RuntimeError(f"invoice {invoice.invoice_id}: the captcha row vanished")
    await set_state(conn, invoice.invoice_id, WAITING_CAPTCHA, rule_id=rule_id, expected=FETCHING_DOCS)
    document = Document(path=str(image), filename=_IMAGE_NAME, mime=_IMAGE_MIME, size=challenge.size)
    await queue_document(
        conn,
        ctx.to,
        document,
        caption=_caption(ctx, invoice, portal_key, rule_id),
        invoice_id=invoice.invoice_id,
        rule_id=rule_id,
    )
    await enqueue(conn, invoice.invoice_id, CAPTCHA_TIMEOUT_STEP, pending.expires_at)


def _caption(ctx: AskContext, invoice: AskedInvoice, portal_key: str, rule_id: str) -> str:
    """Return the BR-F18 or BR-F20 text; BR-F18 names the store's comercio, else the portal key (BR-F18)."""
    if rule_id == RULE_ASK_AGAIN:
        return MESSAGES[RULE_ASK_AGAIN]
    store = ctx.directory.stores.get(invoice.ruc_emisor)
    portal = portal_key if store is None else store.brands[0].comercio
    # shortcut: whole minutes, rounded down, so the sender is never promised more time than the ask has.
    return MESSAGES[RULE_ASK].format(portal=portal, minutes=int(ctx.timeout_s // _SECONDS_PER_MINUTE))


async def drop_image(data_dir: Path, image_path: str) -> None:
    """Delete a captcha image the data directory holds, and its folder when it is then empty (D209).

    A path outside ``data_dir/captchas`` is left alone. Call it after the transaction
    that removed the row has committed, since a rollback cannot bring a file back.
    """
    image = Path(image_path)
    root = data_dir / CAPTCHA_DIRECTORY
    if not image.is_relative_to(root):
        return
    await asyncio.to_thread(image.unlink, missing_ok=True)
    with contextlib.suppress(OSError):
        await asyncio.to_thread(image.parent.rmdir)
