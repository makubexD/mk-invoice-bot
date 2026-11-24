"""Tests for the command menu built from the command registry (D187, R-10.1)."""

from __future__ import annotations

import re

from invoicebot.pipeline.commands import COMMANDS, help_text, menu_entries
from invoicebot.rules.messages_es import HELP_HEADER

#: Telegram's limits for a command name and its description (D187).
COMMAND_NAME = re.compile(r"[a-z0-9_]{1,32}")
MIN_DESCRIPTION = 3
MAX_DESCRIPTION = 256


def test_menu_for_a_sender_names_the_non_admin_commands_in_order() -> None:
    """D187: the default menu lists the sender commands in help order, without the admin ones."""
    assert [name for name, _description in menu_entries(False)] == [
        "ayuda",
        "estado",
        "cancelar",
        "reprocesar",
        "pendientes",
        "pagado",
        "portal",
    ]


def test_menu_for_an_admin_names_every_command_in_order() -> None:
    """D187: an admin's menu lists every registered command in help order."""
    assert [name for name, _description in menu_entries(True)] == list(COMMANDS)


def test_menu_estado_description_drops_the_name_and_argument() -> None:
    """D187: a description is the help line after its first ": "."""
    entries = dict(menu_entries(False))
    assert entries["estado"] == "cómo va el comprobante #12"


def test_menu_entries_fit_telegram_limits() -> None:
    """D187: every command name and description fits Telegram's limits."""
    for is_admin in (False, True):
        for name, description in menu_entries(is_admin):
            assert COMMAND_NAME.fullmatch(name)
            assert MIN_DESCRIPTION <= len(description) <= MAX_DESCRIPTION


def test_help_text_for_an_admin_is_unchanged() -> None:
    """R-10.1: the admin help stays the header followed by every command's line."""
    expected = "\n".join([HELP_HEADER, *(command.help for command in COMMANDS.values())])
    assert help_text(True) == expected
