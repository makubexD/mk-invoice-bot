"""The ``laptop_outlook`` job and receipt file formats (R-7.14, D61).

A job is the JSON the bot drops in the personal OneDrive ``_outbox/``: the mail
to send plus an HMAC over every other field, so only the bot can make the
laptop send. A receipt is the JSON the agent writes back into
``_outbox/receipts/``: the sent time, or the error.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

#: A job id is 32 lowercase hex characters, the form of ``uuid4().hex``.
JOB_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: The largest receipt file the bot reads (4 KiB, R-7.14).
MAX_RECEIPT_BYTES = 4096

#: The longest error text a receipt carries.
MAX_ERROR_LENGTH = 500

_SENT_KEYS = frozenset({"job_id", "sent_at"})
_ERROR_KEYS = frozenset({"job_id", "error"})
#: Characters that would let an attachment name leave the invoice folder on the laptop.
_PATH_CHARS = ("/", "\\", ":", "\x00")


#: One mail the laptop agent sends through Outlook classic (R-7.14).
#:
#: ``attachments`` are plain file names inside the invoice's folder; the agent
#: refuses a job whose HMAC does not verify or whose attachments leave it.
@dataclass(frozen=True)
class LaptopJob:
    """One mail handed to the laptop agent: its id, invoice and content."""

    job_id: str
    invoice_id: int
    to: tuple[str, ...]
    subject: str
    body: str
    attachments: tuple[str, ...]


@dataclass(frozen=True)
class Receipt:
    """What the laptop agent reports back for one job (R-7.14)."""

    job_id: str
    sent_at: datetime | None
    error: str | None


def job_hmac(job: LaptopJob, key: bytes) -> str:
    """Return the hex HMAC-SHA256 of ``job`` under ``key``.

    Args:
        job: the job to sign.
        key: the shared secret; only the bot and the laptop agent hold it.

    Returns:
        The 64-character lowercase hex digest.

    Raises:
        ValueError: the job id or an attachment is not valid.
    """
    return _signature(_job_fields(job), key)


def job_document(job: LaptopJob, key: bytes) -> bytes:
    """Return the job file's bytes: the job's fields plus its HMAC, as JSON.

    Args:
        job: the job to write.
        key: the shared secret the HMAC is computed with.

    Returns:
        Canonical UTF-8 JSON, with lists in place of the job's tuples.

    Raises:
        ValueError: the job id or an attachment is not valid.
    """
    fields = _job_fields(job)
    return _canonical({**fields, "hmac": _signature(fields, key)})


def parse_receipt(data: bytes) -> Receipt:
    """Parse one receipt file written by the laptop agent (R-7.14).

    Args:
        data: the receipt file's bytes, at most 4 KiB.

    Returns:
        The receipt, with either ``sent_at`` or ``error`` set.

    Raises:
        ValueError: the file is too large, is not UTF-8 JSON, has keys other
            than ``job_id`` with ``sent_at`` or ``error``, or a field is
            invalid.
    """
    if len(data) >= MAX_RECEIPT_BYTES:
        raise ValueError(f"receipt is too large: {len(data)} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as err:
        raise ValueError("receipt is not UTF-8") from err
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as err:
        raise ValueError("receipt is not JSON") from err
    if not isinstance(payload, dict):
        raise ValueError("receipt is not a JSON object")
    keys = frozenset(payload)
    if keys not in (_SENT_KEYS, _ERROR_KEYS):
        raise ValueError(f"receipt has unexpected keys: {sorted(keys)!r}")
    job_id = payload["job_id"]
    if not isinstance(job_id, str) or JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError(f"invalid job id {job_id!r}")
    if keys == _SENT_KEYS:
        return Receipt(job_id=job_id, sent_at=_parse_sent_at(payload["sent_at"]), error=None)
    return Receipt(job_id=job_id, sent_at=None, error=_parse_error(payload["error"]))


def _job_fields(job: LaptopJob) -> dict[str, object]:
    """Return the job's signed fields, refusing an invalid id or attachment."""
    if JOB_ID_PATTERN.fullmatch(job.job_id) is None:
        raise ValueError(f"invalid job id {job.job_id!r}")
    for name in job.attachments:
        if not _is_plain_file_name(name):
            raise ValueError(f"invalid attachment {name!r}")
    return {
        "job_id": job.job_id,
        "invoice_id": job.invoice_id,
        "to": list(job.to),
        "subject": job.subject,
        "body": job.body,
        "attachments": list(job.attachments),
    }


def _is_plain_file_name(name: str) -> bool:
    """Return whether ``name`` is a file name inside the invoice's folder."""
    # ":" also refuses a Windows drive-relative name such as ``C:x.pdf`` and an NTFS stream.
    return bool(name) and name not in (".", "..") and not any(char in name for char in _PATH_CHARS)


def _signature(fields: Mapping[str, object], key: bytes) -> str:
    """Return the hex HMAC-SHA256 of the canonical JSON of ``fields``."""
    return hmac.new(key, _canonical(fields), hashlib.sha256).hexdigest()


def _canonical(fields: Mapping[str, object]) -> bytes:
    """Return the canonical ASCII JSON the HMAC and the job file are built from."""
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text.encode("ascii")


def _parse_sent_at(value: object) -> datetime:
    """Return ``value`` as an ISO timestamp with a timezone, or raise."""
    if not isinstance(value, str):
        raise ValueError(f"sent_at is not a string: {value!r}")
    try:
        sent_at = datetime.fromisoformat(value)
    except ValueError as err:
        raise ValueError(f"sent_at is not an ISO timestamp: {value!r}") from err
    if sent_at.tzinfo is None:
        raise ValueError(f"sent_at has no timezone: {value!r}")
    return sent_at


def _parse_error(value: object) -> str:
    """Return ``value`` as an error text of at most 500 characters, or raise."""
    if not isinstance(value, str) or len(value) > MAX_ERROR_LENGTH:
        raise ValueError(f"invalid error {value!r}")
    return value
