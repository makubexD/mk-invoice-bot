"""Tests for the channel-neutral button id codec (R-3.5)."""

from __future__ import annotations

import pytest

from invoicebot.channels.base import ButtonPress
from invoicebot.channels.buttons import (
    ACTION_MAX_CHARS,
    button_id,
    command_button_id,
    is_button_command,
    parse_button_id,
)


def test_button_id_round_trips_through_parse() -> None:
    """R-3.2: a reply id encodes the invoice and the action and parses back."""
    assert button_id(12, "confirm") == "12:confirm"
    assert parse_button_id("12:confirm") == ButtonPress(12, "confirm", None, None)


def test_parse_button_id_bounds_the_invoice_to_nine_digits() -> None:
    """R-3.2: a ten-digit invoice id parses to None; nine digits still parse."""
    assert parse_button_id("9999999999:confirm") is None
    assert parse_button_id("123456789:confirm") == ButtonPress(123456789, "confirm", None, None)


@pytest.mark.parametrize("value", ["x:confirm", "12", "12:Bad-Action", ""])
def test_parse_button_id_returns_none_for_malformed_values(value: str) -> None:
    """R-3.2: a malformed reply id parses to None instead of raising."""
    assert parse_button_id(value) is None


def test_button_id_rejects_bad_action() -> None:
    """R-3.2: an action that is not lowercase words is rejected."""
    with pytest.raises(ValueError, match=r"\[a-z_\]\{1,32\}"):
        button_id(1, "Bad")


def test_button_id_carries_a_choice_and_menu() -> None:
    """R-3.5: a reply id with a choice and menu round-trips through parse."""
    assert button_id(5, "pick", 3, 12) == "5:pick:3:12"
    assert parse_button_id("5:pick:3:12") == ButtonPress(5, "pick", 3, 12)


def test_parse_button_id_without_a_choice_has_no_menu() -> None:
    """R-3.5: a reply id without a choice parses with choice and menu None."""
    assert parse_button_id("5:cancel") == ButtonPress(5, "cancel", None, None)


def test_parse_button_id_refuses_a_three_part_id() -> None:
    """R-3.5: a 3-part id from before the menu field is stale and parses to None."""
    assert parse_button_id("5:pick:3") is None


@pytest.mark.parametrize("value", ["7:pick:1:10000", "7:pick:x:1", "7:Pick:1:1"])
def test_parse_button_id_returns_none_for_bad_choice_or_action(value: str) -> None:
    """R-3.5: a bad choice, menu or upper-case action in a 4-part id parses to None."""
    assert parse_button_id(value) is None


def test_button_id_rejects_choice_outside_0_to_9() -> None:
    """R-3.5: a choice outside 0-9 is rejected."""
    with pytest.raises(ValueError, match="choice must be an int 0-9"):
        button_id(7, "pick", 10, 1)


@pytest.mark.parametrize("menu", [10000, -1, True], ids=["above-max", "negative", "bool"])
def test_button_id_rejects_menu_outside_0_to_9999(menu: int) -> None:
    """R-3.5: a menu outside 0-9999, or a bool, is rejected."""
    with pytest.raises(ValueError, match="menu must be an int 0-9999"):
        button_id(5, "pick", 3, menu)


def test_button_id_requires_a_menu_with_a_choice() -> None:
    """R-3.5: an id with a choice but no menu is rejected."""
    with pytest.raises(ValueError, match="needs a menu"):
        button_id(5, "pick", 3)


def test_button_id_rejects_a_menu_without_a_choice() -> None:
    """R-3.5: an id without a choice cannot carry a menu."""
    with pytest.raises(ValueError, match="menu needs a choice"):
        button_id(5, "cancel", menu=12)


def test_longest_button_id_fits_telegram_callback_data() -> None:
    """R-1.7: the longest id the codec can build fits Telegram's 64-byte callback_data limit."""
    longest = button_id(999_999_999, "a" * ACTION_MAX_CHARS, 9, 9999)
    assert len(longest.encode("utf-8")) <= 64


@pytest.mark.parametrize(
    ("command", "args", "expected"),
    [
        ("reprocesar", ("12",), "/reprocesar 12"),
        ("pendientes", (), "/pendientes"),
    ],
)
def test_command_button_id_builds_the_command_text(
    command: str, args: tuple[str, ...], expected: str
) -> None:
    """D190: a command button's id is the command text, with its argument when given."""
    assert command_button_id(command, *args) == expected


@pytest.mark.parametrize(
    ("command", "arg"),
    [
        ("cancelar", "12"),
        ("reprocesar", "123456789"),
        ("pendientes", ""),
        ("estado", ""),
        ("estado", "7"),
    ],
)
def test_is_button_command_accepts_the_allowlist(command: str, arg: str) -> None:
    """D190: only the allowlisted commands with a valid argument may arrive as a button."""
    assert is_button_command(command, arg)


@pytest.mark.parametrize(
    ("command", "arg"),
    [
        ("cancelar", ""),
        ("cancelar", "12a"),
        ("reprocesar", "1234567890"),
        ("pendientes", "x"),
        ("ayuda", ""),
        ("gap", "ignorar 3"),
        ("portal", "https://x.pe"),
    ],
)
def test_is_button_command_refuses_other_commands(command: str, arg: str) -> None:
    """D190: a command outside the allowlist, or with a bad argument, is refused as a button."""
    assert not is_button_command(command, arg)
