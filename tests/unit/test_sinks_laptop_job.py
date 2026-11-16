"""Tests for the laptop job file and its HMAC (R-7.14, D61)."""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from invoicebot.sinks.laptop_job import (
    RECEIPT_TYPE,
    LaptopJob,
    Receipt,
    job_document,
    job_hmac,
    parse_receipt,
)

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "laptop" / "vectors.json").read_text("utf-8"))
KEY: bytes = VECTORS["key"].encode("ascii")
JOBS: list[dict[str, Any]] = VECTORS["jobs"]

JOB_ID = "0123456789abcdef0123456789abcdef"
JOB_ID_BYTES = JOB_ID.encode("ascii")
RECEIPT_KEY = b"test-receipt-key"
OTHER_KEY = b"another-key"


def _receipt_signature(fields: dict[str, object], key: bytes) -> str:
    """Return the hex HMAC-SHA256 of a receipt's canonical fields."""
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hmac.new(key, text.encode("ascii"), hashlib.sha256).hexdigest()


def _signed(fields: dict[str, object], key: bytes = RECEIPT_KEY) -> bytes:
    """Return a file signed over exactly ``fields``, with nothing added."""
    return json.dumps({**fields, "hmac": _receipt_signature(fields, key)}).encode()


def _signed_receipt(fields: dict[str, object], key: bytes = RECEIPT_KEY) -> bytes:
    """Return the receipt file the laptop agent writes for ``fields``, with its type."""
    return _signed({"type": RECEIPT_TYPE, **fields}, key)


def _job(vector: dict[str, Any]) -> LaptopJob:
    """Build the job a frozen vector describes."""
    return LaptopJob(
        job_id=vector["job_id"],
        created_at=datetime.fromisoformat(vector["created_at"]),
        invoice_id=vector["invoice_id"],
        folder=vector["folder"],
        to=tuple(vector["to"]),
        subject=vector["subject"],
        body=vector["body"],
        attachments=tuple(vector["attachments"]),
    )


@pytest.mark.parametrize("vector", JOBS, ids=lambda vector: vector["job_id"])
def test_vector_hmac_recomputes(vector: dict[str, Any]) -> None:
    """R-7.14: each frozen vector's HMAC recomputes from its fields."""
    assert job_hmac(_job(vector), KEY) == vector["hmac"]


@pytest.mark.parametrize("vector", JOBS, ids=lambda vector: vector["job_id"])
def test_vector_document_round_trips(vector: dict[str, Any]) -> None:
    """R-7.14: the job document is JSON holding the vector's fields and HMAC."""
    assert json.loads(job_document(_job(vector), KEY)) == vector


@pytest.mark.parametrize("name", ["../x.pdf", "a/b.pdf", "a\\b.pdf", "..", "C:x.pdf"])
def test_attachment_outside_invoice_folder_raises(name: str) -> None:
    """R-7.14: an attachment that is not a plain file name is refused."""
    job = replace(_job(JOBS[0]), attachments=(name,))

    with pytest.raises(ValueError, match="invalid attachment"):
        job_document(job, KEY)


@pytest.mark.parametrize("folder", ["../x", "a//b", "/a", "C:x", "a/../b", ""])
def test_folder_outside_invoice_root_raises(folder: str) -> None:
    """R-7.14, D111: a folder that is not a relative path of plain names is refused."""
    job = replace(_job(JOBS[0]), folder=folder)

    with pytest.raises(ValueError, match="invalid folder"):
        job_document(job, KEY)


def test_job_document_signs_its_type_and_creation_time() -> None:
    """R-7.14: the job signs its type and ``created_at`` as UTC seconds."""
    job = replace(
        _job(JOBS[0]),
        created_at=datetime(2026, 11, 16, 7, 0, 0, 987654, tzinfo=timezone(timedelta(hours=-5))),
    )

    document = json.loads(job_document(job, KEY))

    assert document["type"] == "job"
    assert document["created_at"] == "2026-11-16T12:00:00Z"
    assert document["hmac"] == job_hmac(job, KEY)


def test_nested_folder_is_accepted_and_documented() -> None:
    """R-7.14, D111: a nested folder is accepted and appears in the job document."""
    job = _job(JOBS[0])

    assert json.loads(job_document(job, KEY))["folder"] == "Reembolsos/2026-11/F001-123"


def test_bad_job_id_raises() -> None:
    """R-7.14: a job id that is not 32 lowercase hex characters is refused."""
    job = replace(_job(JOBS[0]), job_id="not-a-job-id")

    with pytest.raises(ValueError, match="invalid job id"):
        job_document(job, KEY)


def test_parse_receipt_refuses_a_receipt_without_its_type() -> None:
    """R-7.14: a receipt whose signed keys lack ``type`` is refused."""
    data = _signed({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00"})

    with pytest.raises(ValueError, match="unexpected keys"):
        parse_receipt(data, RECEIPT_KEY)


def test_parse_receipt_refuses_another_type() -> None:
    """R-7.14: a job file renamed into the receipt folder, type ``job``, is refused."""
    data = _signed({"type": "job", "job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00"})

    with pytest.raises(ValueError, match="invalid type"):
        parse_receipt(data, RECEIPT_KEY)


def test_parse_receipt_accepts_signed_sent() -> None:
    """R-7.14: a sent receipt signed with the key parses to its job id and time."""
    data = _signed_receipt({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00"})

    receipt = parse_receipt(data, RECEIPT_KEY)

    assert receipt == Receipt(
        job_id=JOB_ID,
        sent_at=datetime(2026, 11, 16, 10, 30, tzinfo=UTC),
        error=None,
    )


def test_parse_receipt_accepts_signed_error() -> None:
    """R-7.14: an error receipt signed with the key parses to its job id and text."""
    data = _signed_receipt({"job_id": JOB_ID, "error": "Outlook did not start"})

    receipt = parse_receipt(data, RECEIPT_KEY)

    assert receipt == Receipt(job_id=JOB_ID, sent_at=None, error="Outlook did not start")


def test_parse_receipt_refuses_a_wrong_hmac() -> None:
    """R-7.14: a receipt signed with another key is refused."""
    data = _signed_receipt({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00"}, OTHER_KEY)

    with pytest.raises(ValueError, match="hmac"):
        parse_receipt(data, RECEIPT_KEY)


def test_parse_receipt_refuses_a_tampered_field() -> None:
    """R-7.14: changing a signed field without re-signing is refused."""
    fields: dict[str, object] = {"job_id": JOB_ID, "error": "first"}
    signed = json.loads(_signed_receipt(fields))
    signed["error"] = "second"

    with pytest.raises(ValueError, match="hmac"):
        parse_receipt(json.dumps(signed).encode(), RECEIPT_KEY)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        pytest.param(
            b'{"type": "receipt", "job_id": "' + JOB_ID_BYTES + b'", "sent_at": "2026-11-16T10:30:00+00:00"}',
            "unexpected keys",
            id="missing",
        ),
        pytest.param(
            b'{"type": "receipt", "job_id": "'
            + JOB_ID_BYTES
            + b'", "sent_at": "2026-11-16T10:30:00+00:00", "hmac": 3}',
            "hmac is not a string",
            id="not-a-string",
        ),
    ],
)
def test_parse_receipt_refuses_an_unsigned_receipt(data: bytes, match: str) -> None:
    """R-7.14: a receipt with a missing or non-string hmac is refused."""
    with pytest.raises(ValueError, match=match):
        parse_receipt(data, RECEIPT_KEY)


DISTINCTIVE_JOB_ID = "cafebabecafebabecafebabecafebabe"
DISTINCTIVE_ERROR = "DISTINCTIVE-ERROR-VALUE-9f3a"
DISTINCTIVE_TIME = "DISTINCTIVE-SENT-AT-VALUE-7c1d"
DISTINCTIVE_BAD_ID = "DISTINCTIVE-JOB-ID-VALUE-4b2e"


REFUSED_VALUES = [
    pytest.param(
        _signed_receipt({"job_id": DISTINCTIVE_BAD_ID, "error": DISTINCTIVE_ERROR}),
        "invalid job id",
        DISTINCTIVE_BAD_ID,
        id="bad-job-id",
    ),
    pytest.param(
        _signed_receipt({"job_id": DISTINCTIVE_JOB_ID, "error": DISTINCTIVE_ERROR * 20}),
        "invalid error",
        DISTINCTIVE_ERROR,
        id="oversized-error",
    ),
    pytest.param(
        _signed_receipt({"job_id": DISTINCTIVE_JOB_ID, "sent_at": DISTINCTIVE_TIME}),
        "sent_at",
        DISTINCTIVE_TIME,
        id="bad-sent-at",
    ),
    pytest.param(
        _signed_receipt({"job_id": DISTINCTIVE_JOB_ID, "error": DISTINCTIVE_ERROR}, OTHER_KEY),
        "hmac",
        DISTINCTIVE_ERROR,
        id="wrong-hmac",
    ),
]


@pytest.mark.parametrize(("data", "match", "value"), REFUSED_VALUES)
def test_refused_receipt_message_holds_no_field_value(data: bytes, match: str, value: str) -> None:
    """R-7.14: a refused receipt names the field, never its value."""
    with pytest.raises(ValueError, match=match) as caught:
        parse_receipt(data, RECEIPT_KEY)

    assert value not in str(caught.value)


REFUSALS = [
    pytest.param(
        _signed_receipt({"job_id": "not-a-job-id", "error": "x"}),
        "invalid job id",
        id="bad-job-id",
    ),
    pytest.param(
        _signed_receipt({"job_id": JOB_ID, "error": "x" * 501}),
        "invalid error",
        id="oversized-error",
    ),
    pytest.param(b"x" * 4096, "too large", id="oversized-file"),
    pytest.param(
        _signed_receipt({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00", "extra": 1}),
        "unexpected keys",
        id="extra-key",
    ),
    pytest.param(
        _signed_receipt({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00", "error": "x"}),
        "unexpected keys",
        id="sent-at-and-error",
    ),
    pytest.param(
        _signed_receipt({"job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00"}),
        "no timezone",
        id="naive-sent-at",
    ),
]


@pytest.mark.parametrize(("data", "match"), REFUSALS)
def test_parse_receipt_refuses_invalid(data: bytes, match: str) -> None:
    """R-7.14: an invalid receipt raises instead of being trusted."""
    with pytest.raises(ValueError, match=match):
        parse_receipt(data, RECEIPT_KEY)


def test_parse_receipt_refuses_a_non_ascii_hmac_as_a_value_error() -> None:
    """R-7.14: a non-ASCII hmac is refused with ValueError, so the sweep skips it."""
    data = json.dumps(
        {"type": "receipt", "job_id": JOB_ID, "sent_at": "2026-11-16T10:30:00+00:00", "hmac": "ñ" * 64}
    ).encode()

    with pytest.raises(ValueError, match="hmac does not verify"):
        parse_receipt(data, RECEIPT_KEY)


@pytest.mark.parametrize("kind", ["sent", "error"])
def test_parse_receipt_accepts_the_shared_receipt_vectors(kind: str) -> None:
    """R-7.14: the receipts the laptop agent's tests sign verify with the same key here."""
    receipt = VECTORS["receipts"][kind]

    parsed = parse_receipt(json.dumps(receipt).encode(), VECTORS["key"].encode())

    assert parsed.job_id == receipt["job_id"]
