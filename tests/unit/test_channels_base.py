"""Tests for the channel port's profile resolution (R-1.9)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from invoicebot.channels.base import Document, Link, OutboundMessage
from invoicebot.channels.profiles import ProfileIndex
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.outgoing import safe_links

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


@pytest.fixture(scope="module")
def cfg() -> Config:
    return load_config(EXAMPLE)


@pytest.fixture(scope="module")
def index(cfg: Config) -> ProfileIndex:
    return ProfileIndex.from_config(cfg)


def test_same_profile_on_both_channels(index: ProfileIndex) -> None:
    """R-1.9: a profile's WhatsApp number and Telegram id resolve to one profile_id."""
    assert index.resolve("whatsapp", "51999999999") == "yo"
    assert index.resolve("telegram", 100000001) == "yo"


def test_unknown_sender_resolves_to_none(index: ProfileIndex) -> None:
    """R-1.9: an id that is not in the allowlist resolves to None."""
    assert index.resolve("whatsapp", "51900000000") is None
    assert index.resolve("telegram", 999999999) is None


def test_replies_go_to_the_origin_channel(index: ProfileIndex) -> None:
    """R-1.9: a reply about an invoice keeps the channel the invoice came from."""
    assert index.reply_channel("whatsapp") == "whatsapp"
    assert index.reply_channel("telegram") == "telegram"


def test_admin_alerts_go_to_the_preferred_channel(index: ProfileIndex, cfg: Config) -> None:
    """R-1.9: admin alerts go to the owner profile's preferred_channel."""
    assert index.admin_channel(cfg.owner.profile) == "whatsapp"


def test_ids_do_not_cross_channels(index: ProfileIndex) -> None:
    """R-1.9: the same digits on the other channel must not match a profile."""
    assert index.resolve("whatsapp", "100000001") is None
    assert index.resolve("telegram", "51999999999") is None
    assert index.resolve("telegram", "100000001") == "yo"


def test_link_accepts_an_https_url() -> None:
    """D194: a titled https URL with a host is accepted."""
    link = Link(title="x", url="https://onedrive.live.com/?id=1")

    assert link.title == "x"
    assert link.url == "https://onedrive.live.com/?id=1"


@pytest.mark.parametrize(
    "url",
    ["http://onedrive.live.com/?id=1", "javascript:alert(1)", "https:///x"],
)
def test_link_refuses_a_non_https_or_hostless_url(url: str) -> None:
    """D194: a link refuses a URL that is not https or has no host."""
    with pytest.raises(ValidationError):
        Link(title="x", url=url)


@pytest.mark.parametrize(
    ("filename", "size"),
    [("a/b.pdf", 10), ("", 10), ("a.pdf", -1)],
)
def test_document_refuses_a_bad_filename_or_size(filename: str, size: int) -> None:
    """D194: a document refuses a separator in its name, an empty name and a negative size."""
    with pytest.raises(ValidationError):
        Document(path="/data/mail/1/a.pdf", filename=filename, mime="application/pdf", size=size)


def test_outbound_message_round_trips_links_and_document() -> None:
    """D194: links and a document survive the JSON dump and validate."""
    message = OutboundMessage(
        channel="telegram",
        recipient="yo",
        profile_id="yo",
        text="hola",
        links=[Link(title="📂 Abrir carpeta", url="https://x.example/1")],
        document=Document(
            path="/data/mail/1/factura.xlsx",
            filename="factura.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size=10,
        ),
    )

    assert OutboundMessage.model_validate(message.model_dump(mode="json")) == message


def test_safe_links_drops_a_pair_the_validator_refuses() -> None:
    """D194: safe_links keeps the valid pair and drops the refused one."""
    links = safe_links([("a", "https://x.example/1"), ("b", "ftp://x")])

    assert links == [Link(title="a", url="https://x.example/1")]
