"""The button id codec every channel shares (R-3.5); ids fit Telegram's 64-byte ``callback_data``.

An id encodes the invoice and the action as ``"<invoice_id>:<action>"`` and adds an
optional digit choice and its menu number as ``"<invoice_id>:<action>:<choice>:<menu>"``.
A command button's id is the command text: ``"/<command>"`` or ``"/<command> <arg>"``.
"""

from __future__ import annotations

import re

from invoicebot.channels.base import MENU_MAX, ButtonPress

ACTION_MAX_CHARS = 32

#: The prefix of a button id that runs a chat command as if typed (D189).
COMMAND_BUTTON_PREFIX = "/"
#: The most digits an invoice id in a command button's argument may have (D190).
COMMAND_ARG_MAX_DIGITS = 9

#: Commands a button may run with an invoice id as their argument (D190).
_BUTTON_ID_COMMANDS = frozenset({"cancelar", "reprocesar"})
#: Commands a button may run only with no argument (D190).
_BUTTON_NO_ARG_COMMANDS = frozenset({"pendientes"})
#: Commands a button may run with or without an invoice id (D190).
_BUTTON_OPTIONAL_ID_COMMANDS = frozenset({"estado"})

_ACTION_PATTERN = rf"[a-z_]{{1,{ACTION_MAX_CHARS}}}"
_BUTTON_ID_PATTERN = rf"([0-9]{{1,{COMMAND_ARG_MAX_DIGITS}}}):({_ACTION_PATTERN})(?::([0-9]):([0-9]{{1,4}}))?"


def command_button_id(command: str, arg: str = "") -> str:
    """Return the id of a button that runs ``command`` as if typed, with ``arg`` when given (D189)."""
    if not arg:
        return f"{COMMAND_BUTTON_PREFIX}{command}"
    return f"{COMMAND_BUTTON_PREFIX}{command} {arg}"


def is_button_command(command: str, arg: str) -> bool:
    """Return whether ``command`` with ``arg`` is one that may arrive as a button (D190).

    Only ``cancelar`` and ``reprocesar`` with an invoice id, ``pendientes`` with no
    argument, and ``estado`` with no argument or an invoice id are allowed.
    """
    if command in _BUTTON_ID_COMMANDS:
        return _is_command_arg(arg)
    if command in _BUTTON_NO_ARG_COMMANDS:
        return not arg
    if command in _BUTTON_OPTIONAL_ID_COMMANDS:
        return not arg or _is_command_arg(arg)
    return False


def _is_command_arg(arg: str) -> bool:
    """Return whether ``arg`` is a 1- to 9-digit ASCII invoice id (D190)."""
    return arg.isascii() and arg.isdigit() and len(arg) <= COMMAND_ARG_MAX_DIGITS


def button_id(invoice_id: int, action: str, choice: int | None = None, menu: int | None = None) -> str:
    """Encode a reply button id, rejecting a bad action, choice or menu number."""
    if re.fullmatch(_ACTION_PATTERN, action) is None:
        raise ValueError(f"action must match [a-z_]{{1,{ACTION_MAX_CHARS}}}, got {action!r}")
    if choice is None:
        if menu is not None:
            raise ValueError(f"menu needs a choice, got {menu!r}")
        return f"{invoice_id}:{action}"
    if type(choice) is not int or not 0 <= choice <= 9:
        raise ValueError(f"choice must be an int 0-9, got {choice!r}")
    if menu is None:
        raise ValueError(f"choice {choice} needs a menu, got None")
    if type(menu) is not int or not 0 <= menu <= MENU_MAX:
        raise ValueError(f"menu must be an int 0-{MENU_MAX}, got {menu!r}")
    return f"{invoice_id}:{action}:{choice}:{menu}"


def parse_button_id(value: str) -> ButtonPress | None:
    """Parse a reply button id, or None if malformed or a choice lacks its menu."""
    match = re.fullmatch(_BUTTON_ID_PATTERN, value)
    if match is None:
        return None
    choice = match.group(3)
    menu = match.group(4)
    return ButtonPress(
        int(match.group(1)),
        match.group(2),
        int(choice) if choice is not None else None,
        int(menu) if menu is not None else None,
    )
