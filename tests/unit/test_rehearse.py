"""Unit tests for the rehearse request and the ``rehearse replies`` formatter (D165, R-12.8)."""

import pytest

from invoicebot.channels.base import Button
from invoicebot.rehearse import ConsoleReply, RehearseError, RehearseSend, format_replies


def test_format_replies_renders_text_buttons_and_next() -> None:
    """D165: a reply prints its header, text lines, buttons and the next cursor."""
    replies = [
        ConsoleReply(
            id=7,
            rule_id="BR-S09",
            status="sent",
            text="line one\nline two",
            buttons=[Button(id="yes", title="Sí"), Button(id="no", title="No")],
        )
    ]

    assert format_replies(replies, 3) == [
        "#7 BR-S09 sent",
        "line one",
        "line two",
        "[yes] Sí",
        "[no] No",
        "next: --after 7",
    ]


def test_format_replies_without_a_rule_shows_a_dash() -> None:
    """D165: a reply with no rule id prints ``-`` in its header."""
    replies = [ConsoleReply(id=4, rule_id=None, status="pending", text="hola", buttons=[])]

    assert format_replies(replies, 0)[0] == "#4 - pending"


def test_format_replies_with_no_rows_only_gives_the_next_cursor() -> None:
    """D165: an empty reply list prints only the next cursor after ``after_id``."""
    assert format_replies([], 12) == ["next: --after 12"]


def test_format_replies_strips_an_escape_character_from_the_text() -> None:
    """R-12.8: reply text prints without an ESC control character from invoice data."""
    replies = [ConsoleReply(id=3, rule_id=None, status="pending", text="precio \x1b[31mrojo", buttons=[])]

    lines = format_replies(replies, 0)

    assert lines[1] == "precio  [31mrojo"
    assert all("\x1b" not in line for line in lines)


def test_format_replies_strips_a_control_character_from_a_button_title() -> None:
    """R-12.8: a button title carrying a control character prints cleaned."""
    replies = [
        ConsoleReply(
            id=5,
            rule_id="BR-S01",
            status="sent",
            text="",
            buttons=[Button(id="yes", title="Sí\x07")],
        )
    ]

    assert format_replies(replies, 0)[1] == "[yes] Sí"


def test_rehearse_send_refuses_a_file_with_a_button() -> None:
    """D165: a send naming both a file and a button is refused before anything runs."""
    with pytest.raises(RehearseError, match="D165"):
        RehearseSend(name="photo.png", button="yes")
