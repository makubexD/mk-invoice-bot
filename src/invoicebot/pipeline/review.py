"""The pieces of flow B's review before send: the mail's files, its text and its documents (D194).

With the mailer's ``send: review`` the delivery posts the built mail to the chat instead of
sending it. This module writes the attachments under ``<data_dir>/mail/<invoice_id>/``,
renders BR-D19 and queues the attachments as documents. The transaction that opens the
``review`` batch stays in :mod:`invoicebot.pipeline.deliver`, next to the other completions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from psycopg import AsyncConnection

from invoicebot.channels.base import Document
from invoicebot.pipeline.outgoing import Addressee, queue_document
from invoicebot.rules.messages_es import MESSAGES, REVIEW_CC_LINE
from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.mailer import MAIL_DIRECTORY

#: The batch state of a mail posted for the owner to check before it is sent (D194).
REVIEW_BATCH_STATE: Literal["review"] = "review"
#: BR-D19: the rule that posts the mail for review with [Enviar] and [Cancelar] (D194).
RULE_REVIEW = "BR-D19"
#: The flow-data key [Enviar] sets, so the next delivery sends instead of posting again (D194).
REVIEWED_KEY = "reviewed"
#: The flow-data key that holds the digest of the mail the owner was shown (D194).
DIGEST_KEY = "review_digest"


def review_dir(data_dir: Path, invoice_id: int) -> Path:
    """Return the directory that holds the invoice's review files (D194)."""
    return data_dir / MAIL_DIRECTORY / str(invoice_id)


async def write_review_files(
    data_dir: Path, invoice_id: int, attachments: Sequence[Attachment]
) -> list[Document]:
    """Write each attachment into the invoice's review directory and return its document (D194).

    A file already there is replaced, so a retry rewrites the same files.

    Raises:
        ValueError: an attachment name would land outside the review directory.
    """
    directory = review_dir(data_dir, invoice_id)
    documents: list[Document] = []
    for attachment in attachments:
        path = directory / attachment.name
        if not path.resolve().is_relative_to(directory.resolve()) or path.parent != directory:
            raise ValueError("a review attachment name must not leave its directory")
        await asyncio.to_thread(_write, path, attachment.data)
        documents.append(
            Document(
                path=str(path.resolve()),
                filename=attachment.name,
                mime=attachment.mime,
                size=len(attachment.data),
            )
        )
    return documents


async def remove_review_files(data_dir: Path, invoice_id: int) -> None:
    """Delete the invoice's review directory and its files; an absent one is accepted (D194)."""
    await asyncio.to_thread(_remove_tree, review_dir(data_dir, invoice_id))


def _remove_tree(directory: Path) -> None:
    """Remove ``directory`` and everything in it, accepting one that is not there."""
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return


def _write(path: Path, data: bytes) -> None:
    """Create the parent directory and write ``data`` to ``path``, replacing it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def mail_digest(mail: OutgoingMail) -> str:
    """Return the SHA-256 of the mail's recipients, subject, body and attachments (D194).

    Each attachment counts by its name and the SHA-256 of its bytes, so a mail the owner
    reviewed and a mail rebuilt for the send have the same digest only when they are the
    same mail.
    """
    canonical = {
        "to": list(mail.to),
        "cc": list(mail.cc),
        "subject": mail.subject,
        "body": mail.body,
        "attachments": [
            {"name": attachment.name, "sha256": hashlib.sha256(attachment.data).hexdigest()}
            for attachment in mail.attachments
        ],
    }
    text = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def review_text(values: Mapping[str, object], mail: OutgoingMail) -> str:
    """Render BR-D19 for the built mail: recipients, subject, body and file names (D194)."""
    cc_line = REVIEW_CC_LINE.format(cc=", ".join(mail.cc)) if mail.cc else ""
    return MESSAGES[RULE_REVIEW].format(
        serie=values["serie"],
        numero=values["numero"],
        to=", ".join(mail.to),
        cc_line=cc_line,
        subject=mail.subject,
        body=mail.body,
        files=", ".join(attachment.name for attachment in mail.attachments),
    )


async def queue_review_documents(
    conn: AsyncConnection[Any], to: Addressee, invoice_id: int, documents: Sequence[Document]
) -> None:
    """Queue each review file as its own document row, captioned with its name (D194).

    Writes in the caller's transaction, before BR-D19, so its buttons come last.
    """
    for document in documents:
        await queue_document(
            conn, to, document, caption=document.filename, invoice_id=invoice_id, rule_id=RULE_REVIEW
        )
