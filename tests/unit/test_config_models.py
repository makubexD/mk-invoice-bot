"""Tests for the typed config models and the YAML loader (R-9.6)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


def _write_yaml(path: Path, document: Any) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_example_config_loads() -> None:
    """R-9.6: the shipped example config validates against the typed models."""
    cfg = load_config(EXAMPLE)

    assert isinstance(cfg, Config)
    assert cfg.owner.ruc == "20XXXXXXXXX"


def test_unknown_key_fails_naming_path(tmp_path: Path) -> None:
    """R-9.6: an unknown key anywhere is rejected and the error names its dotted path."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["ops"]["backup"]["bogus"] = True
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "ops.backup.bogus" in str(excinfo.value)


def test_placeholder_resolves() -> None:
    """R-9.6: config placeholders such as {owner.ruc} resolve before validation."""
    cfg = load_config(EXAMPLE)

    assert cfg.routing.rules[0].when["num_doc_adq"] == ["20XXXXXXXXX"]
    assert cfg.routing.rules[1].when["num_doc_adq"] == ["20YYYYYYYYY"]


def test_unknown_placeholder_fails(tmp_path: Path) -> None:
    """R-9.6: an unknown placeholder fails the load and names the placeholder."""
    text = EXAMPLE.read_text(encoding="utf-8").replace("{owner.ruc}", "{owner.missing}")
    path = tmp_path / "example.yaml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match=r"owner\.missing"):
        load_config(path)


def test_yaml_tag_is_refused(tmp_path: Path) -> None:
    """R-9.6: YAML tags such as !!python/object are refused by safe_load."""
    path = tmp_path / "tagged.yaml"
    path.write_text("owner: !!python/object:builtins.object {}\n", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_config(path)
