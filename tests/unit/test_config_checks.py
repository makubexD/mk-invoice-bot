"""Tests for the config consistency rules and ``check_config.py`` (R-11.6)."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from invoicebot.config.checks import Problem, flow_problems, label_problems, run_checks
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import (
    ExcelTemplateSink,
    Flow,
    OneDriveFolderSink,
    Portal,
    Store,
    StoreDirectory,
)
from invoicebot.sinks.expr import field_names

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
        "status": "active",
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


def _sol_config_dir(
    tmp_path: Path,
    *,
    status: str,
    login_url: str = "https://api-seguridad.sunat.gob.pe/v1/clientesextranet/login",
    query: dict[str, object] | None = None,
) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    query_document: dict[str, object] = {
        "key": "sunat_sol",
        "adapter": "generic_form",
        "url": "https://e-menu.sunat.gob.pe/consulta-comprobantes",
        "submit": "#btnBuscar",
        "downloads": {"xml": "a:has-text('XML')"},
        "not_found": ".not-found",
    }
    query_document.update(query or {})
    document = {
        "status": status,
        "login": {
            "url": login_url,
            "ruc": "#ruc",
            "user": "#usuario",
            "password": "#clave",
            "submit": "#btnAceptar",
        },
        "query": query_document,
    }
    (config_dir / "sunat_sol.yaml").write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return config_dir


def test_example_config_has_no_problems() -> None:
    """R-9.6: the shipped example config passes every check."""
    cfg = load_config(EXAMPLE)
    empty_directory = StoreDirectory(stores={}, portals={})

    assert run_checks(cfg, empty_directory, changed_portals=frozenset()) == []
    assert check_config.main([str(EXAMPLE)]) == 0


def test_example_config_dni_flow_has_no_blocking_problem() -> None:
    """D142: the shipped personal_dni flow and its BR-R10 rule pass every blocking check."""
    cfg = load_config(EXAMPLE)
    directory = load_directory(ROOT / "config")

    problems = run_checks(cfg, directory, changed_portals=frozenset())

    assert "personal_dni" in cfg.flows
    assert [problem for problem in problems if problem.blocking] == []


def test_repo_config_has_no_blocking_problems() -> None:
    """R-5.8: the shipped config and its store directory pass every blocking check."""
    cfg = load_config(Path("config/example.yaml"))
    directory = load_directory(Path("config"))

    problems = run_checks(cfg, directory, changed_portals=frozenset())

    assert [problem for problem in problems if problem.blocking] == []


def test_repo_sunat_sol_stays_todo_without_a_problem() -> None:
    """R-5.2: the shipped config/ folder loads its todo SOL site and reports no problem for it."""
    cfg = load_config(EXAMPLE)
    directory = load_directory(ROOT / "config")

    assert directory.sunat_sol is not None
    assert directory.sunat_sol.status == "todo"
    assert [
        problem
        for problem in run_checks(cfg, directory, changed_portals=frozenset())
        if "sunat_sol" in problem.path
    ] == []


def test_sol_login_outside_sunat_is_refused_at_load(tmp_path: Path) -> None:
    """R-9.10: a login.url outside the hard-coded SUNAT origins fails load_directory."""
    config_dir = _sol_config_dir(tmp_path, status="todo", login_url="https://evil.test/login")

    with pytest.raises(ValueError, match="not a hard-coded SUNAT origin"):
        load_directory(config_dir)


def test_active_sol_query_gets_the_portal_checks(tmp_path: Path) -> None:
    """R-5.8: an active SOL query with a URL template is blocking and names its file."""
    cfg = load_config(EXAMPLE)
    directory = load_directory(
        _sol_config_dir(
            tmp_path,
            status="active",
            query={"url": "https://e-menu.sunat.gob.pe/{ruc_emisor}"},
        )
    )

    problems = run_checks(cfg, directory, changed_portals=frozenset())

    assert [(problem.code, problem.path, problem.blocking) for problem in problems] == [
        ("PORTAL_URL_TEMPLATE", "sunat_sol.yaml:query.url", True)
    ]


def test_flow_listing_sunat_sol_with_sol_disabled_warns() -> None:
    """R-5.8: a flow that lists sunat_sol while SOL is off gets a non-blocking warning."""
    cfg = load_config(EXAMPLE)
    cfg.sunat.sol.enabled = False

    problems = run_checks(cfg, _directory(), changed_portals=frozenset())

    assert [(problem.code, problem.path, problem.blocking) for problem in problems] == [
        ("SOL_DISABLED", "flows.personal_google.fetchers", False)
    ]


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


def test_todo_and_broken_portals_block_only_when_the_pr_changes_them() -> None:
    """R-11.6: a todo/broken portal blocks only the pull request that changes it; others are reported."""
    cfg = load_config(EXAMPLE)
    directory = _directory(
        _portal(key="pending", status="todo"),
        _portal(key="down", status="broken"),
    )

    on_main = run_checks(cfg, directory, changed_portals=frozenset())
    on_pr = run_checks(cfg, directory, changed_portals=frozenset({"pending"}))

    assert _codes(on_main) == ["PORTAL_NOT_ACTIVE", "PORTAL_NOT_ACTIVE"]
    assert not any(problem.blocking for problem in on_main)
    assert {problem.path: problem.blocking for problem in on_pr} == {
        "portals/pending.yaml:status": True,
        "portals/down.yaml:status": False,
    }


def test_todo_portal_exit_code_depends_on_pr_changed_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-11.6: a todo portal exits 1 only when --pr lists its file; otherwise it is reported."""
    config_path = _config_dir(
        tmp_path, portals=[{"key": "pending", "status": "todo", "adapter": "generic_form"}]
    )

    assert check_config.main([str(config_path), "--pr", "config/portals/pending.yaml"]) == 1
    assert "PORTAL_NOT_ACTIVE" in capsys.readouterr().out
    assert check_config.main([str(config_path), "--pr", "config/stores/20000000001.yaml", "src/x.py"]) == 0
    assert "PORTAL_NOT_ACTIVE" in capsys.readouterr().out
    assert check_config.main([str(config_path), "--pr"]) == 0
    assert check_config.main([str(config_path)]) == 0


def test_python_adapter_outside_portals_package_is_blocking() -> None:
    """R-5.8: a python: adapter outside invoicebot.fetchers.portals.* is rejected."""
    cfg = load_config(EXAMPLE)
    directory = _directory(_portal(adapter="python:invoicebot.fetchers.sites:Adapter"))

    problems = run_checks(cfg, directory, changed_portals=frozenset())

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

    assert (
        run_checks(cfg, _directory(_portal(adapter=adapter)), changed_portals=frozenset({"nubefact"})) == []
    )


def test_unknown_portal_adapter_is_blocking() -> None:
    """R-5.8: any adapter other than generic_form or python: is rejected."""
    cfg = load_config(EXAMPLE)

    problems = run_checks(cfg, _directory(_portal(adapter="selenium")), changed_portals=frozenset())

    assert _codes(problems) == ["PORTAL_ADAPTER_UNKNOWN"]
    assert problems[0].blocking


def test_store_portal_naming_unknown_portal_is_blocking() -> None:
    """R-5.8: a store portal that is neither manual, null nor a portal file is rejected."""
    cfg = load_config(EXAMPLE)
    stores = {"20000000001": _store(portal="ghost")}

    problems = run_checks(cfg, _directory(stores=stores), changed_portals=frozenset())

    assert _codes(problems) == ["STORE_PORTAL_UNKNOWN"]
    assert problems[0].blocking
    assert problems[0].path == "stores/20000000001.yaml:portal"
    assert "ghost" in problems[0].message


@pytest.mark.parametrize("portal", [None, "manual"])
def test_store_portal_manual_or_null_is_accepted(portal: str | None) -> None:
    """R-5.8: 'manual' and null name no portal file and are not errors."""
    cfg = load_config(EXAMPLE)
    stores = {"20000000001": _store(portal=portal)}

    assert run_checks(cfg, _directory(stores=stores), changed_portals=frozenset()) == []


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

    problems = run_checks(cfg, _directory(stores={"20000000001": store}), changed_portals=frozenset())

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

    assert run_checks(cfg, _directory(stores={"20000000001": store}), changed_portals=frozenset()) == []


@pytest.mark.parametrize("missing", ["url", "submit", "downloads", "not_found"])
def test_active_generic_form_missing_a_field_is_blocking(missing: str) -> None:
    """R-5.8: an active generic_form portal must carry every field of its form."""
    cfg = load_config(EXAMPLE)
    portal = _portal(**{missing: None})

    problems = run_checks(cfg, _directory(portal), changed_portals=frozenset())

    assert _codes(problems) == ["PORTAL_FORM_INCOMPLETE"]
    assert problems[0].blocking
    assert problems[0].path == "portals/nubefact.yaml"
    assert missing in problems[0].message


def test_todo_generic_form_missing_fields_has_no_form_problem() -> None:
    """R-5.8: only active generic_form portals must be complete; todo ones are filled later."""
    cfg = load_config(EXAMPLE)
    portal = _portal(status="todo", url=None, submit=None, downloads=None, not_found=None)

    problems = run_checks(cfg, _directory(portal), changed_portals=frozenset())

    assert "PORTAL_FORM_INCOMPLETE" not in _codes(problems)


def test_active_portal_with_url_template_is_refused() -> None:
    """R-5.8: an active portal whose url is still a template is refused at startup."""
    cfg = load_config(EXAMPLE)
    portal = _portal(url="https://portal.example/{ruc_emisor}")

    problems = run_checks(cfg, _directory(portal), changed_portals=frozenset())

    assert _codes(problems) == ["PORTAL_URL_TEMPLATE"]
    assert problems[0].blocking
    assert problems[0].path == "portals/nubefact.yaml:url"


def test_work_flow_llm_fallback_with_deepseek_is_blocking() -> None:
    """D43: work_microsoft can never opt into the DeepSeek LLM fallback."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True

    problems = run_checks(cfg, _directory(), changed_portals=frozenset())

    assert _codes(problems) == ["LLM_WORK_FLOW_DEEPSEEK"]
    assert problems[0].blocking
    assert problems[0].path == "llm_fallback.flows.work_microsoft"


def test_work_flow_llm_fallback_allowed_with_other_provider() -> None:
    """D43: the restriction is specific to the DeepSeek provider."""
    cfg = load_config(EXAMPLE)
    cfg.llm_fallback.flows["work_microsoft"] = True
    cfg.llm_fallback.provider = "openai"

    assert run_checks(cfg, _directory(), changed_portals=frozenset()) == []


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


def test_example_flow_templates_have_no_problems() -> None:
    """BR-D02, R-7.1: the shipped flow B templates pass every flow check."""
    cfg = load_config(EXAMPLE)

    assert flow_problems(cfg, field_names=field_names) == []


def test_flow_label_at_the_cap_is_accepted() -> None:
    """BR-R03: a flow label that fits a WhatsApp button title is accepted."""
    cfg = load_config(EXAMPLE)
    cfg.flows["work_microsoft"].label = "x" * 20

    assert label_problems(cfg, StoreDirectory(stores={}, portals={}), max_chars=20) == []


def test_flow_label_over_the_cap_is_reported() -> None:
    """BR-R03: a flow label longer than a button title is blocking and names the flow."""
    cfg = load_config(EXAMPLE)
    cfg.flows["work_microsoft"].label = "x" * 21

    problems = label_problems(cfg, StoreDirectory(stores={}, portals={}), max_chars=20)

    assert _codes(problems) == ["FLOW_LABEL_LENGTH"]
    assert problems[0].path == "flows.work_microsoft.label"
    assert "work_microsoft" in problems[0].message
    assert "21" in problems[0].message
    assert "20" in problems[0].message
    assert problems[0].blocking


def test_store_comercio_over_the_cap_is_reported() -> None:
    """BR-R03: a comercio longer than a button title is blocking and names the store file."""
    cfg = load_config(EXAMPLE)
    directory = _directory(stores={"20000000001": _store(brands=[{"comercio": "x" * 21}])})

    problems = label_problems(cfg, directory, max_chars=20)

    assert _codes(problems) == ["STORE_COMERCIO_LENGTH"]
    assert problems[0].path == "stores/20000000001.yaml:brands[0].comercio"
    assert "stores/20000000001.yaml" in problems[0].message
    assert "21" in problems[0].message
    assert "20" in problems[0].message
    assert problems[0].blocking


def test_flow_cell_with_unknown_name_is_reported() -> None:
    """BR-D02: a cells expression with an unknown name is a problem carrying its path."""
    cfg = load_config(EXAMPLE)
    sink = cfg.flows["work_microsoft"].sinks[1]
    assert isinstance(sink, ExcelTemplateSink)
    sink.cells["C16"] = "{nope}"

    problems = flow_problems(cfg, field_names=field_names)

    assert [problem.code for problem in problems] == ["FLOW_TEMPLATE_UNKNOWN_NAME"]
    assert problems[0].path == "flows.work_microsoft.sinks[1].cells.C16"
    assert "nope" in problems[0].message
    assert problems[0].blocking


def test_flow_cell_with_forbidden_form_is_reported() -> None:
    """BR-D02: a forbidden field form in a cell is a problem, not a crash."""
    cfg = load_config(EXAMPLE)
    sink = cfg.flows["work_microsoft"].sinks[1]
    assert isinstance(sink, ExcelTemplateSink)
    sink.cells["C16"] = "{x[0]}"

    problems = flow_problems(cfg, field_names=field_names)

    assert [problem.code for problem in problems] == ["FLOW_TEMPLATE_UNKNOWN_NAME"]
    assert problems[0].path == "flows.work_microsoft.sinks[1].cells.C16"
    assert "indexing" in problems[0].message


@pytest.mark.parametrize(
    "folder",
    [
        "{fecha:%Y}-{fecha:%m}-Support_{comercio}",
        "{fecha:%m}-Support_{seq}_{comercio}",
        "{fecha:%Y}-Support_{seq}_{comercio}",
    ],
)
def test_folder_template_missing_part_is_reported(folder: str) -> None:
    """R-7.1: the folder template must use seq and fecha with %Y and %m."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows["work_microsoft"]
    sink = flow.sinks[0]
    assert isinstance(sink, OneDriveFolderSink)
    flow.sinks[0] = sink.model_copy(update={"folder": folder})

    problems = flow_problems(cfg, field_names=field_names)

    assert [problem.code for problem in problems] == ["FLOW_FOLDER_TEMPLATE"]
    assert problems[0].path == "flows.work_microsoft.sinks[0].folder"
    assert problems[0].blocking


@pytest.mark.parametrize("template", ["{motivo}", "{descripcion}"])
def test_description_default_may_not_use_override_names(template: str) -> None:
    """BR-D02: defaults.descripcion may use comercio but not motivo or descripcion."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows["work_microsoft"]
    sink = flow.sinks[1]
    assert isinstance(sink, ExcelTemplateSink)
    flow.sinks[1] = sink.model_copy(
        update={"defaults": sink.defaults.model_copy(update={"descripcion": template})}
    )

    problems = flow_problems(cfg, field_names=field_names)

    assert [problem.code for problem in problems] == ["FLOW_DESCRIPTION_TEMPLATE"]
    assert problems[0].path == "flows.work_microsoft.sinks[1].defaults.descripcion"


def test_mail_attach_without_excel_sink_is_reported() -> None:
    """BR-D02: a mailer may attach excel_template only when the flow has that sink."""
    cfg = load_config(EXAMPLE)
    flow = cfg.flows["work_microsoft"]
    flow.sinks = [sink for sink in flow.sinks if not isinstance(sink, ExcelTemplateSink)]

    problems = flow_problems(cfg, field_names=field_names)

    assert [(problem.code, problem.path) for problem in problems] == [
        ("FLOW_MAIL_ATTACH", "flows.work_microsoft.sinks[1].attach")
    ]
    assert problems[0].blocking


_DRIVE_SINK: dict[str, object] = {
    "kind": "google_drive",
    "account": "owner",
    "root_folder_id": "DRIVE_FOLDER_ID",
    "path": "{fecha:%Y}/{fecha:%m}",
    "filename": "{fecha:%Y-%m-%d}_{serie}-{numero}",
    "upload": ["photo"],
}

_SHEETS_SINK: dict[str, object] = {
    "kind": "google_sheets",
    "account": "owner",
    "spreadsheet_id": "SPREADSHEET_ID",
    "tab": "Compras",
    "columns": {"Total": "{total}"},
}


def _flow_with_columns(columns: dict[str, str], *, drive: bool) -> Flow:
    sheets = dict(_SHEETS_SINK, columns=columns)
    sinks = [_DRIVE_SINK, sheets] if drive else [sheets]
    return Flow.model_validate(
        {
            "label": "Boletas",
            "accepted_tipos": ["03"],
            "fetchers": [],
            "required_docs": [],
            "deliver_without_docs": False,
            "sinks": sinks,
        }
    )


def test_sheets_only_flow_column_with_drive_link_is_blocking() -> None:
    """D144: a Sheets-only flow may not use {link_photo} in a column."""
    cfg = load_config(EXAMPLE)
    cfg.flows = {"personal_dni": _flow_with_columns({"Foto": "{link_photo}"}, drive=False)}

    problems = flow_problems(cfg, field_names=field_names)

    assert _codes(problems) == ["SHEETS_LINK_WITHOUT_DRIVE"]
    assert problems[0].path == "flows.personal_dni.sinks[0].columns.Foto"
    assert "Foto" in problems[0].message
    assert problems[0].blocking


def test_sheets_column_with_drive_link_and_drive_sink_is_accepted() -> None:
    """D144: the same column is accepted when the flow also has a Drive sink."""
    cfg = load_config(EXAMPLE)
    cfg.flows = {"personal_google": _flow_with_columns({"Foto": "{link_photo}"}, drive=True)}

    assert flow_problems(cfg, field_names=field_names) == []


def test_sheets_only_flow_column_without_drive_link_is_accepted() -> None:
    """D144: a Sheets-only flow may use an ordinary name such as {total}."""
    cfg = load_config(EXAMPLE)
    cfg.flows = {"personal_dni": _flow_with_columns({"Total": "{total}"}, drive=False)}

    assert flow_problems(cfg, field_names=field_names) == []
