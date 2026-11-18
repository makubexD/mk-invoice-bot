"""Tests for the single loader shared by startup and reload (R-9.6)."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from invoicebot.runtime_config import (
    REASON_FILE,
    REASON_VALIDATION,
    REASON_YAML,
    ConfigRejected,
    RuntimeConfig,
    load_runtime_config,
)
from invoicebot.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
PARSING_RULES = ROOT / "config" / "parsing_rules.yaml"


def _settings(tmp_path: Path) -> Settings:
    """Copy the example config into a temp config directory with an empty store directory."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    shutil.copy(EXAMPLE, config_dir / "example.yaml")
    (config_dir / "stores").mkdir()
    return Settings(
        config_path=config_dir / "example.yaml",
        parsing_rules_path=PARSING_RULES,
    )


def _example_document() -> dict[str, Any]:
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def _write_config(settings: Settings, document: dict[str, Any]) -> None:
    settings.config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def test_example_config_loads_into_one_runtime_config(tmp_path: Path) -> None:
    """R-9.6: the shipped example config and an empty store directory load as one runtime config."""
    runtime = load_runtime_config(_settings(tmp_path))

    assert isinstance(runtime, RuntimeConfig)
    assert runtime.cfg.owner.ruc == "20XXXXXXXXX"
    assert runtime.directory.stores == {}
    assert runtime.directory.portals == {}
    assert runtime.rules


def test_yaml_syntax_error_is_rejected_as_yaml(tmp_path: Path) -> None:
    """R-9.6: a YAML syntax error rejects the config with the yaml reason."""
    settings = _settings(tmp_path)
    settings.config_path.write_text("owner: [", encoding="utf-8")

    with pytest.raises(ConfigRejected) as info:
        load_runtime_config(settings)

    assert info.value.reason == REASON_YAML
    assert info.value.args == (REASON_YAML,)
    assert str(info.value) == REASON_YAML


def test_missing_config_file_is_rejected_as_file(tmp_path: Path) -> None:
    """R-9.6: a config file that cannot be read (gone mid-edit) rejects the config with the file reason."""
    settings = _settings(tmp_path)
    settings.config_path.unlink()

    with pytest.raises(ConfigRejected) as info:
        load_runtime_config(settings)

    assert info.value.reason == REASON_FILE


def test_pydantic_error_is_rejected_as_validation(tmp_path: Path) -> None:
    """R-9.6: a value pydantic refuses rejects the config with the validation reason."""
    settings = _settings(tmp_path)
    document = _example_document()
    document["sunat"]["validez_api"]["timeout_s"] = "not-a-number"
    _write_config(settings, document)

    with pytest.raises(ConfigRejected) as info:
        load_runtime_config(settings)

    assert info.value.reason == REASON_VALIDATION


def test_blocking_check_problem_is_rejected_with_its_code(tmp_path: Path) -> None:
    """R-9.6: the first blocking check problem rejects the config with that problem's code."""
    settings = _settings(tmp_path)
    store = {
        "ruc": "20000000001",
        "razon_social": "COMERCIO DE PRUEBA S.A.C.",
        "tipo": "restaurante",
        "brands": [{"comercio": "Principal"}],
        "portal": "portal-inexistente",
    }
    stores_dir = settings.config_path.parent / "stores"
    (stores_dir / "20000000001.yaml").write_text(yaml.safe_dump(store, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigRejected) as info:
        load_runtime_config(settings)

    assert info.value.reason == "STORE_PORTAL_UNKNOWN"
    assert info.value.args == ("STORE_PORTAL_UNKNOWN",)


def test_non_blocking_check_problem_still_loads(tmp_path: Path) -> None:
    """R-9.6: a reported but non-blocking check problem does not reject the config."""
    settings = _settings(tmp_path)
    portals_dir = settings.config_path.parent / "portals"
    portals_dir.mkdir()
    portal = {
        "adapter": "generic_form",
        "status": "todo",
        "url": "https://portal.example/consulta",
    }
    (portals_dir / "portal-nuevo.yaml").write_text(yaml.safe_dump(portal, sort_keys=False), encoding="utf-8")

    runtime = load_runtime_config(settings)

    assert set(runtime.directory.portals) == {"portal-nuevo"}
