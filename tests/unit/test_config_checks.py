"""Tests for the config consistency rules and ``check_config.py`` (R-11.6)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from invoicebot.config.checks import Problem, run_checks
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import Portal, Store, StoreDirectory

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
    values: dict[str, object] = {
        "key": "nubefact",
        "adapter": "generic_form",
        "url": "https://portal.example/consulta",
        "submit": "button[type=submit]",
        "downloads": {"pdf": "button.pdf", "xml": "button.xml"},
        "not_found": ".not-found",
    }
    values.update(overrides)
    return Portal.model_validate(values)


def _store(**overrides: object) -> Store:
    values: dict[str, object] = {
        "ruc": "20000000001",
        "razon_social": "COMERCIO DE PRUEBA S.A.C.",
        "tipo": "restaurante",
        "brands": [{"comercio": "Principal"}],
        "portal": None,
    }
    values.update(overrides)
    return Store.model_validate(values)


def _directory(*portals: Portal, stores: dict[str, Store] | None = None) -> StoreDirectory:
    return StoreDirectory(
        stores=stores or {},
        portals={portal.key: portal for portal in portals},
    )


def _config_dir(tmp_path: Path, *, portals: list[dict[str, object]] | None = None) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "stores").mkdir()
    config_path = config_dir / "example.yaml"
    shutil.copy(EXAMPLE, config_path)
    if portals is not None:
        portals_dir = config_dir / "portals"
        portals_dir.mkdir()
        for portal in portals:
            document = {key: value for key, value in portal.items() if key != "key"}
            (portals_dir / f"{portal['key']}.yaml").write_text(
                yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
            )
    return config_path


def _codes(problems: list[Problem]) -> list[str]:
    return [problem.code for problem in problems]


def test_example_config_has_no_problems() -> None:
    """R-9.6: the shipped example config passes every check."""
    cfg = load_config(EXAMPLE)
    empty_directory = StoreDirectory(stores={}, portals={})

    assert run_checks(cfg, empty_directory, pr_mode=False) == []
    assert check_config.main([str(EXAMPLE)]) == 0


def test_repo_config_has_no_blocking_problems() -> None:
    """R-5.8: the shipped config and its store directory pass every blocking check."""
    cfg = load_config(Path("config/example.yaml"))
    directory = load_directory(Path("config"))

    problems = run_checks(cfg, directory, pr_mode=False)

    assert [problem for problem in problems if problem.blocking] == []


def test_invalid_config_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-9.6: a config that fails validation exits 1 and prints the error."""
    config_path = _config_dir(tmp_path)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["bogus"] = True
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert check_config.main([str(config_path)]) == 1
    assert "bogus" in capsys.readouterr().err


def test_routing_rule_id_outside_catalog_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-11.1: every routing rule id must exist in the business-rule catalog."""
    config_path = _config_dir(tmp_path)
    document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    document["routing"]["rules"][0]["id"] = "BR-R99"
    config_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert check_config.main([str(config_path)]) == 1
    output = capsys.readouterr().out
    assert "ROUTING_RULE_UNKNOWN" in output
    assert "BR-R99" in output


def test_todo_and_broken_portals_block_only_in_pr_mode() -> None:
    """R-11.6: todo/broken portals are blocking on pull requests and reported on main."""
    cfg = load_config(EXAMPLE)
    directory = _directory(
        _portal(key="pending", status="todo"),
        _portal(key="down", status="broken"),
    )

    on_main = run_checks(cfg, directory, pr_mode=False)
    on_pr = run_checks(cfg, directory, pr_mode=True)

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
    directory = _directory(_portal(adapter="python:invoicebot.fetchers.sites:Adapter"))

    problems = run_checks(cfg, directory, pr_mode=False)

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

    assert run_checks(cfg, _directory(_portal(adapter=adapter)), pr_mode=True) == []


def test_unknown_portal_adapter_is_blocking() -> None:
    """R-5.8: any adapter other than generic_form or python: is rejected."""
    cfg = load_config(EXAMPLE)

    problems = run_checks(cfg, _directory(_portal(adapter="selenium")), pr_mode=False)

    assert _codes(problems) == ["PORTAL_ADAPTER_UNKNOWN"]
    assert problems[0].blocking


def test_store_portal_naming_unknown_portal_is_blocking() -> None:
    """R-5.8: a store portal that is neither manual, null nor a portal file is rejected."""
    cfg = load_config(EXAMPLE)
    stores = {"20000000001": _store(portal="ghost")}

    problems = run_checks(cfg, _directory(stores=stores), pr_mode=False)

    assert _codes(problems) == ["STORE_PORTAL_UNKNOWN"]
    assert problems[0].blocking
    assert problems[0].path == "stores/20000000001.yaml:portal"
    assert "ghost" in problems[0].message


@pytest.mark.parametrize("portal", [None, "manual"])
def test_store_portal_manual_or_null_is_accepted(portal: str | None) -> None:
    """R-5.8: 'manual' and null name no portal file and are not errors."""
    cfg = load_config(EXAMPLE)
    stores = {"20000000001": _store(portal=portal)}

    assert run_checks(cfg, _directory(stores=stores), pr_mode=False) == []


@pytest.mark.parametrize(
    ("first_prefix", "second_prefix"),
    [
        ("F01", "F01"),
        ("F01", "F014"),
        ("F014", "F01"),
    ],
)
def test_overlapping_serie_prefixes_across_brands_is_blocking(first_prefix: str, second_prefix: str) -> None:
    """R-5.8: series match by prefix, so two brands may not share overlapping prefixes."""
    cfg = load_config(EXAMPLE)
    store = _store(
        brands=[
            {"comercio": "Uno", "serie_prefix": [first_prefix]},
            {"comercio": "Dos", "serie_prefix": [second_prefix]},
        ]
    )

    problems = run_checks(cfg, _directory(stores={"20000000001": store}), pr_mode=False)

    assert _codes(problems) == ["STORE_SERIE_PREFIX_OVERLAP"]
    assert problems[0].blocking
    assert problems[0].path == "stores/20000000001.yaml:serie_prefix"
    assert "Uno" in problems[0].message
    assert "Dos" in problems[0].message
    assert f"'{first_prefix}'" in problems[0].message
    assert f"'{second_prefix}'" in problems[0].message


def test_same_brand_or_disjoint_serie_prefixes_is_accepted() -> None:
    """R-5.8: prefixes within one brand, or disjoint across brands, never collide."""
    cfg = load_config(EXAMPLE)
    store = _store(
        brands=[
            {"comercio": "Uno", "serie_prefix": ["F01", "F014"]},
            {"comercio": "Dos", "serie_prefix": ["B01"]},
        ]
    )

    assert run_checks(cfg, _directory(stores={"20000000001": store}), pr_mode=False) == []


@pytest.mark.parametrize("missing", ["url", "submit", "downloads", "not_found"])
def test_active_generic_form_missing_a_field_is_blocking(missing: str) -> None:
    """R-5.8: an active generic_form portal must carry every field of its form."""
    cfg = load_config(EXAMPLE)
    portal = _portal(**{missing: None})

    problems = run_checks(cfg, _directory(portal), pr_mode=False)

    assert _codes(problems) == ["PORTAL_FORM_INCOMPLETE"]
    assert problems[0].blocking
    assert problems[0].path == "portals/nubefact.yaml"
    assert missing in problems[0].message


def test_todo_generic_form_missing_fields_has_no_form_problem() -> None:
    """R-5.8: only active generic_form portals must be complete; todo ones are filled later."""
    cfg = load_config(EXAMPLE)
    portal = _portal(status="todo", url=None, submit=None, downloads=None, not_found=None)

    problems = run_checks(cfg, _directory(portal), pr_mode=False)

    assert "PORTAL_FORM_INCOMPLETE" not in _codes(problems)


def test_work_flow_llm_fallback_with_deepseek_is_blocking() -> None:
    """D43: work_microsoft can never opt into the DeepSeek LLM fallback."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True

    problems = run_checks(cfg, _directory(), pr_mode=False)

    assert _codes(problems) == ["LLM_WORK_FLOW_DEEPSEEK"]
    assert problems[0].blocking
    assert problems[0].path == "llm_fallback.flows.work_microsoft"


def test_work_flow_llm_fallback_allowed_with_other_provider() -> None:
    """D43: the restriction is specific to the DeepSeek provider."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True
    cfg.llm_fallback.provider = "openai"

    assert run_checks(cfg, _directory(), pr_mode=False) == []


def test_script_rejects_outside_adapter_on_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """R-5.8: the CLI exits 1 for an outside-package adapter even without --pr."""
    config_path = _config_dir(
        tmp_path,
        portals=[{"key": "bad", "adapter": "python:invoicebot.fetchers.sites:Adapter"}],
    )

    assert check_config.main([str(config_path)]) == 1
    assert "PORTAL_ADAPTER_OUTSIDE_PACKAGE" in capsys.readouterr().out


def test_config_without_portal_folder_passes(tmp_path: Path) -> None:
    """R-9.6: a config directory without stores/ or portals/ entries still validates."""
    config_path = _config_dir(tmp_path)

    assert check_config.main([str(config_path)]) == 0
