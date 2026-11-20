"""Drift tests for the documentation diagrams, command list and links (D161)."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from invoicebot import states
from invoicebot.cli import _parser

ROOT = Path(__file__).resolve().parents[2]
ARCHITECTURE = ROOT / "docs" / "architecture.md"
_MERMAID_OPEN = "```mermaid"
_FENCE = "```"
_EXCLUDED_PACKAGES = frozenset({"events"})
_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_DOC_LINKS = ("README.md", "docs/architecture.md", "docs/runbook.md", "deploy/README.md")


def _mermaid_blocks(text: str) -> list[str]:
    """Return the body of each ``mermaid`` fenced block in ``text``."""
    blocks: list[str] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() == _MERMAID_OPEN:
            body: list[str] = []
            index += 1
            while index < len(lines) and lines[index].strip() != _FENCE:
                body.append(lines[index])
                index += 1
            blocks.append("\n".join(body))
        index += 1
    return blocks


def _first_block(text: str, marker: str) -> str:
    """Return the first mermaid block whose body starts with ``marker``."""
    for body in _mermaid_blocks(text):
        if body.strip().startswith(marker):
            return body
    raise AssertionError(f"no mermaid block starts with {marker}")


def _names_whole_word(text: str, names: list[str]) -> list[str]:
    """Return the names missing from ``text`` as a whole word."""
    return [name for name in names if re.search(rf"\b{re.escape(name)}\b", text) is None]


def _has_node(body: str, name: str) -> bool:
    """True when ``body`` defines a node with id ``name``, not just a label that mentions it."""
    return re.search(rf"^\s*{re.escape(name)}\s*[\[(]", body, re.MULTILINE) is not None


def _state_names() -> list[str]:
    """Return the module-level state constants, whose value equals their name."""
    return sorted(value for name, value in vars(states).items() if isinstance(value, str) and value == name)


def _package_names() -> list[str]:
    """Return the packages under ``src/invoicebot`` the component diagram must name."""
    package_root = ROOT / "src" / "invoicebot"
    return sorted(
        path.name
        for path in package_root.iterdir()
        if path.is_dir()
        and path.name != "__pycache__"
        and path.name not in _EXCLUDED_PACKAGES
        and (path / "__init__.py").is_file()
    )


def _subcommands() -> list[str]:
    """Return the subcommand names of the ``invoicebot`` parser."""
    for action in _parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    raise AssertionError("invoicebot has no subcommands")


def test_state_diagram_names_every_state() -> None:
    """D161 the state diagram names every state in ``invoicebot.states``."""
    body = _first_block(ARCHITECTURE.read_text(encoding="utf-8"), "stateDiagram-v2")
    names = _state_names()

    assert names, "no state constants found in invoicebot.states"
    missing = _names_whole_word(body, names)
    assert not missing, f"states missing from the diagram: {missing}"


def test_component_diagram_names_every_package() -> None:
    """D161 the component diagram names every package under ``src/invoicebot``."""
    body = _first_block(ARCHITECTURE.read_text(encoding="utf-8"), "flowchart")
    names = _package_names()

    assert names, "no packages found under src/invoicebot"
    missing = [name for name in names if not _has_node(body, name)]
    assert not missing, f"packages without a node in the diagram: {missing}"


def test_readme_lists_every_cli_command() -> None:
    """D161 the README names every ``invoicebot`` subcommand."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    names = _subcommands()

    assert names, "invoicebot has no subcommands"
    missing = [name for name in names if f"invoicebot {name}" not in text]
    assert not missing, f"commands missing from README.md: {missing}"


@pytest.mark.parametrize("doc", _DOC_LINKS)
def test_doc_links_resolve(doc: str) -> None:
    """D161 every relative markdown link in the docs points at a file on disk."""
    path = ROOT / doc
    text = path.read_text(encoding="utf-8")
    broken = []
    for target in _LINK.findall(text):
        if target.startswith("#") or _SCHEME.match(target):
            continue
        relative = target.split("#", 1)[0]
        if relative and not (path.parent / relative).exists():
            broken.append(target)
    assert not broken, f"{doc}: broken links {broken}"
