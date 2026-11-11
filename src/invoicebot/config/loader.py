"""YAML config loader with load-time placeholder resolution (R-9.6)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from invoicebot.config.models import Config

# Dotted, brace-wrapped paths without a format spec, e.g. "{owner.ruc}".
# Single-name ({total}) and format-spec ({fecha:%Y}) tokens are runtime template
# fields owned by the sinks, not config references.
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+)\}")


def load_config(path: str | Path) -> Config:
    """Read, resolve and validate the config file (R-9.6).

    Only ``yaml.safe_load`` is used, so arbitrary YAML tags are refused.
    """
    raw = Path(path).read_text(encoding="utf-8")
    document = yaml.safe_load(raw)
    if not isinstance(document, dict):
        raise ValueError(f"config root must be a mapping, got {type(document).__name__}")
    return Config.model_validate(_resolve_placeholders(document))


def _resolve_placeholders(document: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _resolve_node(value, document, in_routing=(key == "routing")) for key, value in document.items()
    }


def _resolve_node(node: Any, document: dict[str, Any], *, in_routing: bool) -> Any:
    if isinstance(node, str):
        return _resolve_string(node, document, in_routing=in_routing)
    if isinstance(node, dict):
        return {key: _resolve_node(value, document, in_routing=in_routing) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_node(item, document, in_routing=in_routing) for item in node]
    return node


def _resolve_string(value: str, document: dict[str, Any], *, in_routing: bool) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        found, resolved = _lookup(document, path)
        if found:
            return resolved
        # The routing section holds config references, so a dotted path there
        # must resolve. Other sections (flows/sinks) hold runtime templates
        # whose dotted tokens look similar but are evaluated per invoice.
        if in_routing:
            raise ValueError(f"Unknown placeholder '{{{path}}}' in config")
        return match.group(0)

    return _PLACEHOLDER.sub(replace, value)


def _lookup(document: dict[str, Any], path: str) -> tuple[bool, str]:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, ""
        current = current[part]
    if isinstance(current, (dict, list)):
        raise ValueError(f"Placeholder '{{{path}}}' does not resolve to a scalar")
    return True, str(current)
