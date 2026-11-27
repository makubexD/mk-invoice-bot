"""Tests for the invoice mail builder and its delivery modes (R-7.6, R-7.7, D69, D89)."""

from __future__ import annotations

import email
import email.policy
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from email.message import Message
from pathlib import Path

import httpx
import pytest

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MailerSink
from invoicebot.sinks.base import Attachment, OutgoingMail
from invoicebot.sinks.laptop_job import LaptopJob, job_hmac
from invoicebot.sinks.mailer import DryRun, InboxRule, LaptopOutlook, MailerSend, build_mail, mail_modes
from invoicebot.sinks.mailer_graph import GraphMailer

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"

PDF = Attachment("invoice.pdf", b"%PDF-1.4 synthetic", "application/pdf")
XLSX = Attachment(
    "invoice.xlsx",
    b"PK\x03\x04 synthetic",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
)
XML = Attachment("invoice.xml", b"<Invoice/>", "application/xml")
FILES = {"excel_template": XLSX, "pdf": PDF, "xml": XML}

SINK = MailerSink(
    kind="mailer",
    trigger="per_invoice",
    modes=["dry_run"],
    account="personal",
    to=["ops@example.com"],
    cc=["jefa@example.com"],
    subject="Support release {fecha:%d/%m/%Y}",
    body="Hola, adjunto los documentos.",
    attach=["excel_template", "pdf", "xml"],
)

FOLDER = "/Reembolsos/2026-11/001 - Synthetic"

MAIL = OutgoingMail(
    to=("ops@example.com",),
    cc=("jefa@example.com",),
    subject="Support release 05/11/2026",
    body="Hola, adjunto los documentos.",
    attachments=(XLSX, PDF, XML),
)


class FakeMailer:
    """A mailer that records every mail it is asked to send."""

    def __init__(self) -> None:
        self.sent: list[OutgoingMail] = []

    async def send(self, mail: OutgoingMail) -> None:
        self.sent.append(mail)


class FakeTokens:
    """A TokenSource that always hands out the same synthetic token."""

    async def token(self) -> str:
        return "synthetic-bearer-value"


def _parse(data: bytes) -> Message:
    return email.message_from_bytes(data, policy=email.policy.default)


def _read_eml(path: str) -> Message:
    return _parse(Path(path).read_bytes())


def _config(send_enabled: bool) -> Config:
    """Load example.yaml with real sending on or off; on requires production (R-7.7)."""
    cfg = load_config(EXAMPLE)
    mail = cfg.mail.model_copy(update={"send_enabled": send_enabled})
    env = "production" if send_enabled else cfg.env
    return cfg.model_copy(update={"mail": mail, "env": env})


def test_build_mail_renders_subject_and_orders_attachments() -> None:
    """R-7.6, D69: the subject is the rendered template and files follow attach order."""
    mail = build_mail(SINK, {"fecha": date(2026, 11, 5)}, FILES)

    assert mail.to == ("ops@example.com",)
    assert mail.cc == ("jefa@example.com",)
    assert mail.subject == "Support release 05/11/2026"
    assert mail.body == "Hola, adjunto los documentos."
    assert [attachment.name for attachment in mail.attachments] == [
        "invoice.xlsx",
        "invoice.pdf",
        "invoice.xml",
    ]


def test_build_mail_with_missing_attachment_raises() -> None:
    """R-7.6: an attachment the sink asks for but the invoice lacks fails loudly."""
    files = {"excel_template": XLSX, "pdf": PDF}

    with pytest.raises(ValueError, match="missing attachment 'xml'"):
        build_mail(SINK, {"fecha": date(2026, 11, 5)}, files)


async def test_dry_run_writes_parseable_eml(tmp_path: Path) -> None:
    """R-7.7, D69: the assisted mode writes an .eml the owner can open and send."""
    mode = DryRun(tmp_path)

    path = await mode.deliver(7, MAIL, FOLDER)

    assert mode.completion == "tap"
    assert path == str(tmp_path / "mail" / "7.eml")
    message = _read_eml(path)
    assert message["Subject"] == "Support release 05/11/2026"
    assert message["To"] == "ops@example.com"
    assert [part.get_filename() for part in message.iter_attachments()] == [
        "invoice.xlsx",
        "invoice.pdf",
        "invoice.xml",
    ]


async def test_dry_run_second_call_replaces_the_eml(tmp_path: Path) -> None:
    """D69: a rewritten mail replaces the earlier draft instead of adding a file."""
    mode = DryRun(tmp_path)
    first = await mode.deliver(7, MAIL, FOLDER)
    second = await mode.deliver(7, replace(MAIL, subject="Support release 06/11/2026"), FOLDER)

    assert first == second
    assert _read_eml(second)["Subject"] == "Support release 06/11/2026"
    assert [path.name for path in (tmp_path / "mail").iterdir()] == ["7.eml"]


def test_mail_modes_close_sending_when_disabled(tmp_path: Path) -> None:
    """R-7.7, D89: with send_enabled false the graph entry is replaced by the dry run."""
    dry_run = DryRun(tmp_path)
    graph = MailerSend(FakeMailer())

    modes = mail_modes(_config(send_enabled=False), dry_run, graph, FakeMailer(), None, None)

    assert modes["dry_run"] is dry_run
    assert modes["graph"] is dry_run
    assert modes["graph"].completion == "tap"


async def test_mail_modes_send_once_through_graph_when_enabled(tmp_path: Path) -> None:
    """R-7.7, D89: in production the graph entry sends the mail exactly once."""
    mailer = FakeMailer()
    dry_run = DryRun(tmp_path)
    graph = MailerSend(mailer)

    modes = mail_modes(_config(send_enabled=True), dry_run, graph, FakeMailer(), None, None)
    result = await modes["graph"].deliver(7, MAIL, FOLDER)

    assert modes["graph"] is graph
    assert modes["graph"].completion == "accepted"
    assert result is None
    assert mailer.sent == [MAIL]


def test_built_modes_report_their_completion_and_rule(tmp_path: Path) -> None:
    """R-7.8, D108: each built mode says how it completes and which rule reports it."""
    modes = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), FakeMailer(), None, None
    )

    assert (modes["dry_run"].completion, modes["dry_run"].done_rule) == ("tap", "BR-D08")
    assert (modes["graph"].completion, modes["graph"].done_rule) == ("accepted", "BR-D10")


def test_mail_modes_without_a_bot_mailer_offer_no_inbox_rule(tmp_path: Path) -> None:
    """R-7.7, D108, D164: a config that never signs the bot mailbox in gets no inbox_rule mode."""
    modes = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), None, None, None
    )

    assert "inbox_rule" not in modes
    assert set(modes) == {"dry_run", "graph"}


async def test_inbox_rule_sends_only_to_the_owner_address() -> None:
    """R-7.7, D108: the Graph request replaces the sink's HR recipients with the owner's address."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        mailer = GraphMailer(http, FakeTokens(), 5.0, account="bot")
        mode = InboxRule(mailer, "owner@example.com")
        await mode.deliver(7, MAIL, FOLDER)

    assert len(requests) == 1
    assert requests[0].url.path.endswith("/me/sendMail")
    message = json.loads(requests[0].content)["message"]
    assert message["toRecipients"] == [{"emailAddress": {"address": "owner@example.com"}}]
    assert message["ccRecipients"] == []


def test_inbox_rule_reports_accepted_and_br_d13(tmp_path: Path) -> None:
    """R-7.7, D108: the inbox_rule mode completes when Graph accepts it, under BR-D13."""
    modes = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), FakeMailer(), None, None
    )

    assert (modes["inbox_rule"].completion, modes["inbox_rule"].done_rule) == ("accepted", "BR-D13")


def test_inbox_rule_closes_to_the_dry_run_when_sending_is_disabled(tmp_path: Path) -> None:
    """R-7.7, D89: with send_enabled false the inbox_rule entry is the dry run."""
    dry_run = DryRun(tmp_path)

    modes = mail_modes(
        _config(send_enabled=False), dry_run, MailerSend(FakeMailer()), FakeMailer(), None, None
    )

    assert modes["inbox_rule"] is dry_run


def test_inbox_rule_is_absent_without_the_owner_address(tmp_path: Path) -> None:
    """R-7.7: without mail.owner_work_address there is no inbox_rule mode."""
    cfg = _config(send_enabled=True)
    cfg = cfg.model_copy(update={"mail": cfg.mail.model_copy(update={"owner_work_address": None})})

    modes = mail_modes(cfg, DryRun(tmp_path), MailerSend(FakeMailer()), FakeMailer(), None, None)

    assert "inbox_rule" not in modes


class FakeDrive:
    """A drive that records every upload at its absolute path."""

    def __init__(self) -> None:
        self.uploads: dict[str, bytes] = {}

    async def list_names(self, path: str) -> list[str]:
        """Return nothing: the laptop mode only uploads."""
        return []

    async def create_folder(self, parent: str, name: str) -> None:
        """Accept the creation; the laptop mode only uploads."""

    async def upload(self, path: str, data: bytes) -> None:
        """Keep the file's bytes at its absolute path."""
        self.uploads[path] = data

    async def move(self, path: str, new_parent: str, new_name: str) -> None:
        """Accept the move; the laptop mode only uploads."""


LAPTOP_KEY = b"synthetic-laptop-key"
LAPTOP_ROOT = "/Personal"


def _fixed_clock() -> datetime:
    """Return the fixed time every laptop job is signed with."""
    return datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def test_laptop_outlook_reports_receipt_and_br_d10() -> None:
    """R-7.14, D108: the laptop mode completes on a receipt and reports under BR-D10."""
    mode = LaptopOutlook(FakeDrive(), LAPTOP_ROOT, LAPTOP_KEY, _fixed_clock)

    assert (mode.completion, mode.done_rule) == ("receipt", "BR-D10")


async def test_laptop_outlook_writes_one_signed_job() -> None:
    """R-7.14, D111: deliver uploads one job whose HMAC verifies and whose folder is the invoice's."""
    drive = FakeDrive()
    mode = LaptopOutlook(drive, LAPTOP_ROOT, LAPTOP_KEY, _fixed_clock)

    job_id = await mode.deliver(7, MAIL, FOLDER)

    assert len(job_id) == 32
    assert all(char in "0123456789abcdef" for char in job_id)
    assert list(drive.uploads) == [f"{LAPTOP_ROOT}/_outbox/{job_id}.json"]
    payload = json.loads(drive.uploads[f"{LAPTOP_ROOT}/_outbox/{job_id}.json"])
    assert payload["type"] == "job"
    assert payload["created_at"] == "2026-11-16T12:00:00Z"
    assert payload["folder"] == "Reembolsos/2026-11/001 - Synthetic"
    assert payload["attachments"] == ["invoice.xlsx", "invoice.pdf", "invoice.xml"]
    job = LaptopJob(
        job_id=payload["job_id"],
        created_at=datetime.fromisoformat(payload["created_at"]),
        invoice_id=payload["invoice_id"],
        folder=payload["folder"],
        to=tuple(payload["to"]),
        subject=payload["subject"],
        body=payload["body"],
        attachments=tuple(payload["attachments"]),
    )
    assert payload["hmac"] == job_hmac(job, LAPTOP_KEY)


def test_mail_modes_add_laptop_outlook_only_when_given(tmp_path: Path) -> None:
    """R-7.14: laptop_outlook exists only when app.py built the mode."""
    without = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), FakeMailer(), None, None
    )
    with_laptop = mail_modes(
        _config(send_enabled=True),
        DryRun(tmp_path),
        MailerSend(FakeMailer()),
        FakeMailer(),
        LaptopOutlook(FakeDrive(), LAPTOP_ROOT, LAPTOP_KEY, _fixed_clock),
        None,
    )

    assert "laptop_outlook" not in without
    assert with_laptop["laptop_outlook"] is not with_laptop["dry_run"]
    assert with_laptop["laptop_outlook"].completion == "receipt"


def test_mail_modes_map_laptop_outlook_to_the_dry_run_when_disabled(tmp_path: Path) -> None:
    """R-7.14, R-7.7: with send_enabled false the laptop_outlook entry is the dry run."""
    dry_run = DryRun(tmp_path)

    modes = mail_modes(
        _config(send_enabled=False),
        dry_run,
        MailerSend(FakeMailer()),
        FakeMailer(),
        LaptopOutlook(FakeDrive(), LAPTOP_ROOT, LAPTOP_KEY, _fixed_clock),
        None,
    )

    assert modes["laptop_outlook"] is dry_run


def test_mail_modes_map_smtp_to_its_mailer_when_sending_is_on(tmp_path: Path) -> None:
    """D205: with sending on, the smtp entry is the given smtp mailer."""
    smtp = MailerSend(FakeMailer())

    modes = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), None, None, smtp
    )

    assert modes["smtp"] is smtp


def test_mail_modes_map_smtp_to_the_dry_run_when_sending_is_off(tmp_path: Path) -> None:
    """D205: with sending off, the smtp entry is the dry run."""
    dry_run = DryRun(tmp_path)

    modes = mail_modes(
        _config(send_enabled=False), dry_run, MailerSend(FakeMailer()), None, None, MailerSend(FakeMailer())
    )

    assert modes["smtp"] is dry_run


def test_mail_modes_have_no_smtp_without_an_smtp_mailer(tmp_path: Path) -> None:
    """D205: without an smtp mailer, there is no smtp mode."""
    modes = mail_modes(
        _config(send_enabled=True), DryRun(tmp_path), MailerSend(FakeMailer()), None, None, None
    )

    assert "smtp" not in modes
