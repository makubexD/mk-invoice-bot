"""Tests for the laptop job file and its HMAC (R-7.14, D61)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from invoicebot.sinks.laptop_job import LaptopJob, Receipt, job_document, job_hmac, parse_receipt

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "laptop" / "vectors.json").read_text("utf-8"))
KEY: bytes = VECTORS["key"].encode("ascii")
JOBS: list[dict[str, Any]] = VECTORS["jobs"]

JOB_ID = "0123456789abcdef0123456789abcdef"
JOB_ID_BYTES = JOB_ID.encode("ascii")


def _job(vector: dict[str, Any]) -> LaptopJob:
    """Build the job a frozen vector describes."""
    return LaptopJob(
        job_id=vector["job_id"],
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


def test_nested_folder_is_accepted_and_documented() -> None:
    """R-7.14, D111: a nested folder is accepted and appears in the job document."""
    job = _job(JOBS[0])

    assert json.loads(job_document(job, KEY))["folder"] == "Reembolsos/2026-11/F001-123"


def test_bad_job_id_raises() -> None:
    """R-7.14: a job id that is not 32 lowercase hex characters is refused."""
    job = replace(_job(JOBS[0]), job_id="not-a-job-id")

    with pytest.raises(ValueError, match="invalid job id"):
        job_document(job, KEY)


def test_parse_receipt_accepts_sent() -> None:
    """R-7.14: a receipt with sent_at parses to its job id and time."""
    receipt = parse_receipt(b'{"job_id": "' + JOB_ID_BYTES + b'", "sent_at": "2026-11-16T10:30:00+00:00"}')

    assert receipt == Receipt(
        job_id=JOB_ID,
        sent_at=datetime(2026, 11, 16, 10, 30, tzinfo=UTC),
        error=None,
    )


def test_parse_receipt_accepts_error() -> None:
    """R-7.14: a receipt with error parses to its job id and text."""
    receipt = parse_receipt(b'{"job_id": "' + JOB_ID_BYTES + b'", "error": "Outlook did not start"}')

    assert receipt == Receipt(job_id=JOB_ID, sent_at=None, error="Outlook did not start")


REFUSALS = [
    pytest.param(
        b'{"job_id": "not-a-job-id", "error": "x"}',
        "invalid job id",
        id="bad-job-id",
    ),
    pytest.param(
        json.dumps({"job_id": JOB_ID, "error": "x" * 501}).encode(),
        "invalid error",
        id="oversized-error",
    ),
    pytest.param(b"x" * 4096, "too large", id="oversized-file"),
    pytest.param(
        b'{"job_id": "' + JOB_ID_BYTES + b'", "sent_at": "2026-11-16T10:30:00+00:00", "extra": 1}',
        "unexpected keys",
        id="extra-key",
    ),
    pytest.param(
        b'{"job_id": "' + JOB_ID_BYTES + b'", "sent_at": "2026-11-16T10:30:00+00:00", "error": "x"}',
        "unexpected keys",
        id="sent-at-and-error",
    ),
    pytest.param(
        b'{"job_id": "' + JOB_ID_BYTES + b'", "sent_at": "2026-11-16T10:30:00"}',
        "no timezone",
        id="naive-sent-at",
    ),
]


@pytest.mark.parametrize(("data", "match"), REFUSALS)
def test_parse_receipt_refuses_invalid(data: bytes, match: str) -> None:
    """R-7.14: an invalid receipt raises instead of being trusted."""
    with pytest.raises(ValueError, match=match):
        parse_receipt(data)
