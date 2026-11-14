"""R-9.8 / R-11.1: every chat text has one home in ``rules/messages_es.py`` (S5.3).

The checker walks the package with :mod:`ast` and fails on a call to
``queue_message``, ``OutboundMessage`` or ``Button`` that passes a string
literal, or an f-string with a literal part holding a letter, as its text: such
a text belongs in the catalog, not in the code that sends it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "invoicebot"

#: The one module allowed to hold user-facing texts.
MESSAGES_MODULE = SRC / "rules" / "messages_es.py"

#: The calls whose text argument must come from the catalog.
TEXT_CALLS = frozenset({"queue_message", "OutboundMessage", "Button"})

#: The keywords that carry the text of one of those calls.
TEXT_KEYWORDS = frozenset({"text", "title"})


def _call_name(call: ast.Call) -> str | None:
    """Return the called function's name, or ``None`` when it is not a plain name."""
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _text_arguments(call: ast.Call) -> list[ast.expr]:
    """Return the expressions holding a call's text, ``queue_message``'s positional one included."""
    arguments = [keyword.value for keyword in call.keywords if keyword.arg in TEXT_KEYWORDS]
    if _call_name(call) == "queue_message" and len(call.args) > 2:
        arguments.append(call.args[2])
    return arguments


def _holds_letters(value: str) -> bool:
    """Return whether ``value`` holds at least one letter."""
    return any(character.isalpha() for character in value)


def _is_hardcoded_text(node: ast.expr) -> bool:
    """Return whether ``node`` is literal user-facing text."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and _holds_letters(node.value)
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(part, ast.Constant) and isinstance(part.value, str) and _holds_letters(part.value)
            for part in node.values
        )
    return False


def hardcoded_text_lines(source: str) -> list[int]:
    """Return the lines of ``source`` whose text call hard-codes user-facing text."""
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _call_name(node) in TEXT_CALLS
        and any(_is_hardcoded_text(argument) for argument in _text_arguments(node))
    ]


def _package_sources() -> list[Path]:
    """Return every module of the package except the message catalog."""
    return sorted(path for path in SRC.rglob("*.py") if path != MESSAGES_MODULE)


def test_package_hardcodes_no_user_text() -> None:
    """R-9.8 / R-11.1: no module outside the catalog hard-codes chat text."""
    offenders = {
        str(path.relative_to(SRC)): lines
        for path in _package_sources()
        if (lines := hardcoded_text_lines(path.read_text(encoding="utf-8")))
    }

    assert offenders == {}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param('queue_message(conn, to, "Hola")', [1], id="queue-message-positional"),
        pytest.param('queue_message(conn, to, text="Hola")', [1], id="queue-message-keyword"),
        pytest.param('queue_message(conn, to, text=MESSAGES["BR-S01"])', [], id="catalog-lookup"),
        pytest.param('Button(id="x", title=f"Ver {n}")', [1], id="fstring-with-literal"),
        pytest.param('Button(id="x", title=f"{n}")', [], id="fstring-only-fields"),
    ],
)
def test_source_with_text_is_flagged(source: str, expected: list[int]) -> None:
    """R-9.8 / R-11.1: a literal or mixed f-string text is flagged; a catalog lookup is not."""
    assert hardcoded_text_lines(source) == expected
