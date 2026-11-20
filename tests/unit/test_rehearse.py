"""Unit tests for the ``rehearse replies`` formatter (D165)."""

from invoicebot.channels.base import Button
from invoicebot.rehearse import ConsoleReply, format_replies


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
