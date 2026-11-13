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


def test_empty_igv_rates_fails(tmp_path: Path) -> None:
    """R-2.4: extraction.igv_rates must name at least one rate."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["igv_rates"] = []
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.igv_rates" in str(excinfo.value)


def test_accepted_tipos_outside_supported_tipos_fails_naming_flow(tmp_path: Path) -> None:
    """R-3.1: a flow may only accept tipos listed in extraction.supported_tipos."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["accepted_tipos"] = ["01", "09"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="work_microsoft"):
        load_config(path)


def test_routing_rule_with_unknown_flow_fails_naming_it(tmp_path: Path) -> None:
    """R-3.1: a routing rule that names a flow missing from flows fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["routing"]["rules"][0]["flow"] = "no_such_flow"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="no_such_flow"):
        load_config(path)


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


def test_zero_helper_timeout_fails(tmp_path: Path) -> None:
    """R-9.2: extraction.helper_timeout_s must be a positive timeout."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["helper_timeout_s"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.helper_timeout_s" in str(excinfo.value)


def test_zero_max_image_px_fails(tmp_path: Path) -> None:
    """R-9.6: extraction.max_image_px must be a positive pixel bound."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["max_image_px"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.max_image_px" in str(excinfo.value)


def test_review_threshold_above_one_fails(tmp_path: Path) -> None:
    """R-9.6: extraction.review_threshold is a confidence fraction in [0, 1]."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["review_threshold"] = 1.5
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.review_threshold" in str(excinfo.value)


def test_routing_rule_with_unknown_when_key_fails(tmp_path: Path) -> None:
    """R-3.1: a routing rule may only match on the reading fields the router knows."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["routing"]["rules"][0]["when"] = {"num_doc_adq_": ["20XXXXXXXXX"]}
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "routing.rules.0.when" in str(excinfo.value)
