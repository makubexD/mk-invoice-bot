"""Tests for the config consistency rules and ``check_config.py`` (R-11.6)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from invoicebot.config.checks import Problem, run_checks
from invoicebot.config.loader import load_config, load_portals
from invoicebot.config.models import Portal

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
SCRIPT = ROOT / "scripts" / "check_config.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_config", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_config = _load_script()


def _portal(**overrides: object) -> Portal:
    values: dict[str, object] = {"key": "nubefact", "adapter": "generic_form"}
    values.update(overrides)
    return Portal.model_validate(values)


def _config_dir(tmp_path: Path, *, portals: list[dict[str, object]] | None = None) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_path = config_dir / "example.yaml"
    shutil.copy(EXAMPLE, config_path)
    if portals is not None:
        (config_dir / "portals.yaml").write_text(yaml.safe_dump(portals, sort_keys=False), encoding="utf-8")
    return config_path


def _codes(problems: list[Problem]) -> list[str]:
    return [problem.code for problem in problems]


def test_example_config_has_no_problems() -> None:
    """R-9.6: the shipped example config passes every check."""
    cfg = load_config(EXAMPLE)

    assert run_checks(cfg, [], pr_mode=False) == []
    assert check_config.main([str(EXAMPLE)]) == 0


def test_invalid_config_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-9.6: a config that fails validation exits 1 and prints the error."""
    config_path = _config_dir(tmp_path)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["bogus"] = True
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert check_config.main([str(config_path)]) == 1
    assert "bogus" in capsys.readouterr().err


def test_todo_and_broken_portals_block_only_in_pr_mode() -> None:
    """R-11.6: todo/broken portals are blocking on pull requests and reported on main."""
    cfg = load_config(EXAMPLE)
    portals = [
        _portal(key="pending", status="todo"),
        _portal(key="down", status="broken"),
    ]

    on_main = run_checks(cfg, portals, pr_mode=False)
    on_pr = run_checks(cfg, portals, pr_mode=True)

    assert _codes(on_main) == ["PORTAL_NOT_ACTIVE", "PORTAL_NOT_ACTIVE"]
    assert not any(problem.blocking for problem in on_main)
    assert all(problem.blocking for problem in on_pr)


def test_todo_portal_exit_code_depends_on_pr_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-11.6: a todo portal exits 1 with --pr and 0 without it."""
    config_path = _config_dir(
        tmp_path, portals=[{"key": "pending", "status": "todo", "adapter": "generic_form"}]
    )

    assert check_config.main(["--pr", str(config_path)]) == 1
    assert "PORTAL_NOT_ACTIVE" in capsys.readouterr().out
    assert check_config.main([str(config_path)]) == 0
    assert "PORTAL_NOT_ACTIVE" in capsys.readouterr().out


def test_python_adapter_outside_portals_package_is_blocking() -> None:
    """R-5.8: a python: adapter outside invoicebot.fetchers.portals.* is rejected."""
    cfg = load_config(EXAMPLE)
    portals = [_portal(adapter="python:invoicebot.fetchers.sites:Adapter")]

    problems = run_checks(cfg, portals, pr_mode=False)

    assert _codes(problems) == ["PORTAL_ADAPTER_OUTSIDE_PACKAGE"]
    assert problems[0].blocking


@pytest.mark.parametrize(
    "adapter",
    [
        "generic_form",
        "python:invoicebot.fetchers.portals.nubefact:Adapter",
        "python: invoicebot.fetchers.portals.sunat:Adapter",
    ],
)
def test_accepted_portal_adapters(adapter: str) -> None:
    """R-5.8: generic_form and python: adapters inside the portals package are accepted."""
    cfg = load_config(EXAMPLE)

    assert run_checks(cfg, [_portal(adapter=adapter)], pr_mode=True) == []


def test_unknown_portal_adapter_is_blocking() -> None:
    """R-5.8: any adapter other than generic_form or python: is rejected."""
    cfg = load_config(EXAMPLE)

    problems = run_checks(cfg, [_portal(adapter="selenium")], pr_mode=False)

    assert _codes(problems) == ["PORTAL_ADAPTER_UNKNOWN"]
    assert problems[0].blocking


def test_work_flow_llm_fallback_with_deepseek_is_blocking() -> None:
    """D43: work_microsoft can never opt into the DeepSeek LLM fallback."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True

    problems = run_checks(cfg, [], pr_mode=False)

    assert _codes(problems) == ["LLM_WORK_FLOW_DEEPSEEK"]
    assert problems[0].blocking
    assert problems[0].path == "llm_fallback.flows.work_microsoft"


def test_work_flow_llm_fallback_allowed_with_other_provider() -> None:
    """D43: the restriction is specific to the DeepSeek provider."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True
    cfg.llm_fallback.provider = "openai"

    assert run_checks(cfg, [], pr_mode=False) == []


def test_load_portals_missing_file_returns_empty(tmp_path: Path) -> None:
    """R-5.8: the portal registry is optional, so a missing file is an empty list."""
    assert load_portals(tmp_path / "portals.yaml") == []


def test_load_portals_reads_registry(tmp_path: Path) -> None:
    """R-5.8: portals.yaml is a YAML list validated by the Portal model."""
    registry = tmp_path / "portals.yaml"
    registry.write_text(
        yaml.safe_dump(
            [
                {
                    "key": "nubefact",
                    "status": "active",
                    "match": {"ruc": ["20XXXXXXXXX"], "url_contains": ["nubefact"]},
                    "adapter": "generic_form",
                    "url": "https://example.test/buscar",
                    "fields": {"#ruc": "{ruc_emisor}", "#tipo": {"select": "{tipo}"}},
                    "captcha": {"image": "#captchaImg", "input": "#captcha", "solver": ["tesseract", "user"]},
                    "submit": "#btnBuscar",
                    "downloads": {"xml": "a:has-text('XML')", "pdf": "a:has-text('PDF')"},
                }
            ],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    portals = load_portals(registry)

    assert len(portals) == 1
    assert portals[0].key == "nubefact"
    assert portals[0].fields["#tipo"] == {"select": "{tipo}"}
    assert portals[0].captcha is not None
    assert portals[0].captcha.solver == ["tesseract", "user"]


def test_script_rejects_outside_adapter_on_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-5.8: the CLI exits 1 for an outside-package adapter even without --pr."""
    config_path = _config_dir(
        tmp_path,
        portals=[{"key": "bad", "adapter": "python:invoicebot.fetchers.sites:Adapter"}],
    )

    assert check_config.main([str(config_path)]) == 1
    assert "PORTAL_ADAPTER_OUTSIDE_PACKAGE" in capsys.readouterr().out


def test_config_without_portals_file_passes(tmp_path: Path) -> None:
    """R-9.6: a config directory without portals.yaml still validates."""
    config_path = _config_dir(tmp_path)

    assert check_config.main([str(config_path)]) == 0
