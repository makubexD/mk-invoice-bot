"""Tests for the typed config models and the YAML loader (R-9.6)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import time, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from invoicebot.auth.providers import microsoft_accounts
from invoicebot.config.loader import load_config, load_directory
from invoicebot.config.models import (
    CalendarField,
    ChannelTemplates,
    Config,
    ConsoleChannel,
    FlowInbox,
    GapsGithub,
    GoogleDriveSink,
    GoogleSheetsSink,
    Microsoft,
    Padron,
    PickField,
    Portal,
    WhatsAppChannel,
    google_accounts_in,
    parse_delay,
)

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
CONFIG_DIR = ROOT / "config"

#: From tests/synthetic_rucs.txt; no real RUC may appear here.
SYNTHETIC_RUC = "20000000001"


def _write_yaml(path: Path, document: Any) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _store_document(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "ruc": SYNTHETIC_RUC,
        "razon_social": "EJEMPLO RESTAURANTES S.A.C.",
        "tipo": "restaurante",
        "brands": [{"comercio": "LaLena"}],
        "portal": "example",
    }
    document.update(overrides)
    return document


def _write_store(config_dir: Path, name: str, document: dict[str, Any]) -> Path:
    stores = config_dir / "stores"
    stores.mkdir(parents=True, exist_ok=True)
    return _write_yaml(stores / f"{name}.yaml", document)


def _write_portal(config_dir: Path, name: str, document: dict[str, Any]) -> Path:
    portals = config_dir / "portals"
    portals.mkdir(parents=True, exist_ok=True)
    return _write_yaml(portals / f"{name}.yaml", document)


def test_example_config_loads() -> None:
    """R-9.6, R-6.1, R-6.2: the shipped example config, Google sink ids included, validates."""
    cfg = load_config(EXAMPLE)

    assert isinstance(cfg, Config)
    assert cfg.owner.ruc == "20XXXXXXXXX"


def test_example_config_has_google_settings() -> None:
    """R-6.2: the example config names the OAuth client and each Google sink's account."""
    cfg = load_config(EXAMPLE)

    assert cfg.google is not None
    assert cfg.google.client_id.endswith(".apps.googleusercontent.com")
    assert cfg.google.timeout_s == 30
    assert cfg.google.max_rows == 20000
    drive, sheets = cfg.flows["personal_google"].sinks
    assert isinstance(drive, GoogleDriveSink)
    assert isinstance(sheets, GoogleSheetsSink)
    assert (drive.account, sheets.account) == ("owner", "owner")


def test_example_config_maps_each_google_account_to_its_mailbox() -> None:
    """R-6.5: the example pins each sink and gmail inbox account to its mailbox (D138)."""
    cfg = load_config(EXAMPLE)

    assert cfg.google is not None
    assert cfg.google.accounts == {
        "owner": "owner@example.com",
        "invoices_a": "invoices@example.com",
    }


def test_google_sink_naming_an_unlisted_account_fails(tmp_path: Path) -> None:
    """R-6.5: a sink's account must be a key of google.accounts (D138)."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["google"]["accounts"]["owner"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="owner"):
        load_config(path)


def test_gmail_inbox_naming_an_unlisted_account_fails(tmp_path: Path) -> None:
    """R-6.5: a gmail inbox's account must be a key of google.accounts (D138)."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["google"]["accounts"]["invoices_a"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="invoices_a"):
        load_config(path)


def test_google_account_address_without_an_at_sign_fails(tmp_path: Path) -> None:
    """R-6.5: an account's mailbox is one address, so a value without '@' fails (D138)."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["google"]["accounts"]["owner"] = "not-an-address"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="owner"):
        load_config(path)


def test_google_account_address_is_stored_lower_cased(tmp_path: Path) -> None:
    """R-6.5: an account's mailbox is lower-cased, so the pinned address compares case-free."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["google"]["accounts"]["owner"] = "Owner@Example.COM"
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    assert cfg.google is not None
    assert cfg.google.accounts["owner"] == "owner@example.com"


def test_google_accounts_in_collects_both_sinks_and_a_gmail_inbox(tmp_path: Path) -> None:
    """R-6.5, D138: a flow's Google accounts are its drive sink's, sheets sink's and gmail inbox's."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["google"]["accounts"].update({"drive_a": "drive@example.com", "sheets_a": "sheets@example.com"})
    sinks = document["flows"]["personal_google"]["sinks"]
    sinks[0]["account"] = "drive_a"
    sinks[1]["account"] = "sheets_a"
    path = _write_yaml(tmp_path / "example.yaml", document)

    flow = load_config(path).flows["personal_google"]

    assert set(google_accounts_in(flow)) == {"drive_a", "invoices_a", "sheets_a"}


def test_google_accounts_in_skips_a_graph_inbox() -> None:
    """R-6.5, D138: a Graph inbox names a Microsoft account, so it is no Google account."""
    flow = load_config(EXAMPLE).flows["work_microsoft"]

    assert google_accounts_in(flow) == []


def test_example_config_maps_each_microsoft_account_to_its_mailbox() -> None:
    """R-7.9, D164: the example pins every Microsoft account a flow uses to its mailbox."""
    cfg = load_config(EXAMPLE)

    assert microsoft_accounts(cfg) <= set(cfg.microsoft.accounts)


@pytest.mark.parametrize(
    ("account", "change"),
    [
        ("bot", "inbox"),
        ("personal", "drive"),
        ("personal", "backup"),
    ],
)
def test_microsoft_account_a_flow_or_the_backup_uses_must_be_pinned(
    tmp_path: Path, account: str, change: str
) -> None:
    """R-7.9, D119, D164: a used Microsoft account without a mailbox in microsoft.accounts fails at load.

    ``bot`` is used by the example's Graph inbox; ``personal`` by its OneDrive target and,
    with no Microsoft flow at all, still by the nightly backup.
    """
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["microsoft"]["accounts"][account]
    if change == "backup":
        document["flows"] = {
            name: flow for name, flow in document["flows"].items() if name != "work_microsoft"
        }
        document["routing"]["rules"] = [
            rule for rule in document["routing"]["rules"] if rule.get("flow") != "work_microsoft"
        ]
        document["microsoft"]["accounts"].pop("bot", None)
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=f"microsoft.accounts has no mailbox for {account}"):
        load_config(path)


def test_microsoft_single_account_without_inbox_rule_loads(tmp_path: Path) -> None:
    """R-7.9, D164: one pinned ``personal`` account serves the drive, the graph send and the inbox."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    flow = document["flows"]["work_microsoft"]
    flow["email_inbox"]["account"] = "personal"
    for sink in flow["sinks"]:
        if sink["kind"] == "mailer":
            sink["modes"] = ["graph"]
    document["microsoft"]["accounts"] = {"personal": "personal@example.com"}
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    assert set(cfg.microsoft.accounts) == {"personal"}


def test_microsoft_account_name_not_lowercase_fails() -> None:
    """R-7.9, D164: an account name is a key, so one with an upper-case letter fails."""
    with pytest.raises(ValidationError, match="Bot"):
        Microsoft(
            client_id="00000000-0000-0000-0000-000000000000",
            timeout_s=30,
            accounts={"Bot": "bot@example.com"},
        )


def test_microsoft_account_address_without_an_at_sign_fails() -> None:
    """R-7.9, D164: an account's mailbox is one address, so a value without '@' fails."""
    with pytest.raises(ValidationError, match="bot"):
        Microsoft(
            client_id="00000000-0000-0000-0000-000000000000",
            timeout_s=30,
            accounts={"bot": "not-an-address"},
        )


def test_microsoft_account_address_is_stored_lower_cased() -> None:
    """R-7.9, D164: an account's mailbox is lower-cased, so the pinned address compares case-free."""
    microsoft = Microsoft(
        client_id="00000000-0000-0000-0000-000000000000",
        timeout_s=30,
        accounts={"bot": "Bot@Example.com"},
    )

    assert microsoft.accounts["bot"] == "bot@example.com"


def test_google_max_message_bytes_must_be_positive(tmp_path: Path) -> None:
    """R-5.2: the full-message read cap is a positive size, so zero fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["google"]["max_message_bytes"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="max_message_bytes"):
        load_config(path)


def test_sheets_sink_with_the_key_header_fails(tmp_path: Path) -> None:
    """R-6.3: column A is the natural key, so a configured Clave column is refused."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][1]["columns"]["Clave"] = "{serie}-{numero}"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="Clave"):
        load_config(path)


@pytest.mark.parametrize("tab", ["", "x" * 101, "Compras/2026", "Compras[1]", "Compras?"])
def test_sheets_sink_with_a_tab_title_sheets_refuses_fails(tmp_path: Path, tab: str) -> None:
    """R-6.2: an empty, too long or forbidden-character tab title fails at load, not at Google."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][1]["tab"] = tab
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="tab"):
        load_config(path)


def test_routing_rule_naming_an_unset_owner_dni_fails(tmp_path: Path) -> None:
    """D143, BR-R10: a rule on {owner.dni} with dni unset fails at load instead of matching "None"."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["owner"]["dni"] = None
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValueError, match=r"owner\.dni"):
        load_config(path)


def test_sheets_sink_without_a_drive_sink_loads(tmp_path: Path) -> None:
    """D144: a sheets sink alone writes the row, so a flow without a drive sink loads."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["flows"]["personal_google"]["sinks"][0]
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    kinds = [sink.kind for sink in cfg.flows["personal_google"].sinks]
    assert kinds == ["google_sheets"]


def test_drive_sink_without_a_sheets_sink_fails(tmp_path: Path) -> None:
    """D144: a drive sink renders links the sheets sink holds, so a drive sink alone is refused."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["flows"]["personal_google"]["sinks"][1]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="needs a 'google_sheets' sink"):
        load_config(path)


def test_drive_sink_before_a_sheets_sink_loads() -> None:
    """D144: a drive sink followed by a sheets sink loads with both."""

    cfg = load_config(EXAMPLE)

    kinds = [sink.kind for sink in cfg.flows["personal_google"].sinks]
    assert kinds == ["google_drive", "google_sheets"]


def test_drive_root_folder_id_that_is_not_a_google_id_fails(tmp_path: Path) -> None:
    """R-6.1, R-6.2: a root folder id is one Google id, so a path fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][0]["root_folder_id"] = "a/b"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="root_folder_id"):
        load_config(path)


def test_spreadsheet_id_that_is_not_a_google_id_fails(tmp_path: Path) -> None:
    """R-6.1, R-6.2: a spreadsheet id is one Google id, so a value with a space fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][1]["spreadsheet_id"] = "x y"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="spreadsheet_id"):
        load_config(path)


def test_drive_root_folder_id_may_be_the_literal_root(tmp_path: Path) -> None:
    """R-6.1, R-6.2: the literal 'root' names the user's drive root and is accepted."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][0]["root_folder_id"] = "root"
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    drive = cfg.flows["personal_google"].sinks[0]
    assert isinstance(drive, GoogleDriveSink)
    assert drive.root_folder_id == "root"


def test_gmail_inbox_without_google_settings_fails(tmp_path: Path) -> None:
    """R-6.5: a gmail inbox uses the bot's Google OAuth client, so google is required."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"] = []
    del document["google"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="gmail"):
        load_config(path)


def test_leftover_upsert_key_is_refused(tmp_path: Path) -> None:
    """R-6.3: the sheets key is always the natural key, so upsert_key is refused."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"][1]["upsert_key"] = "natural_key"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="upsert_key"):
        load_config(path)


def test_example_config_has_daily_times_and_ops_defaults() -> None:
    """R-9.6: the example config names its daily times and ops limits as typed values (D114)."""
    cfg = load_config(EXAMPLE)

    assert cfg.gaps.digest_at == time(8, 0)
    assert cfg.ops.backup.at == time(3, 0)
    assert cfg.ops.backup.max_mb == 200
    assert cfg.ops.self_alerts.realert_hours == 6


def test_example_config_has_pdf_and_validez_settings() -> None:
    """R-9.6: the example config names the PDF limits and the validez API endpoints."""
    cfg = load_config(EXAMPLE)

    assert cfg.limits.media.max_pdf_pages == 5
    assert cfg.extraction.pdf_min_text_chars == 40
    assert cfg.sunat.validez_api.enabled is False
    assert "{client_id}" in cfg.sunat.validez_api.token_url
    assert "{ruc}" in cfg.sunat.validez_api.api_url
    assert cfg.sunat.validez_api.timeout_s == 10


def test_zero_max_pdf_pages_fails(tmp_path: Path) -> None:
    """R-2.15: limits.media.max_pdf_pages is a positive page bound, so 0 fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["limits"]["media"]["max_pdf_pages"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "limits.media.max_pdf_pages" in str(excinfo.value)


def test_plain_http_token_url_fails(tmp_path: Path) -> None:
    """R-4.1: the token endpoint is only called over TLS, so an http:// URL fails at load."""
    plain_http_endpoint = "http://api-seguridad.sunat.gob.pe/v1/clientesextranet/{client_id}/oauth2/token/"
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["sunat"]["validez_api"]["token_url"] = plain_http_endpoint
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "sunat.validez_api.token_url" in str(excinfo.value)


def test_api_url_without_the_ruc_placeholder_fails(tmp_path: Path) -> None:
    """R-4.1: the validate URL fills the RUC, so a template without {ruc} fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["sunat"]["validez_api"]["api_url"] = (
        "https://api.sunat.gob.pe/v1/contribuyente/validarcomprobante"
    )
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "sunat.validez_api.api_url" in str(excinfo.value)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("admins", ["yo"]),
        ("digest_cron", "0 8 * * *"),
    ],
)
def test_removed_gaps_key_is_refused(tmp_path: Path, key: str, value: Any) -> None:
    """R-9.6: ``gaps.admins`` and ``gaps.digest_cron`` are gone, so the load refuses them (D113, D114)."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["gaps"][key] = value
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert f"gaps.{key}" in str(excinfo.value)


def test_digest_at_outside_the_clock_fails(tmp_path: Path) -> None:
    """R-9.6: ``gaps.digest_at`` is a time of day, so 25:00 fails at load (D114)."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["gaps"]["digest_at"] = "25:00"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "gaps.digest_at" in str(excinfo.value)


def test_unknown_digest_kind_fails(tmp_path: Path) -> None:
    """R-11.8: the daily digest names only known gap kinds, so an unknown one fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["gaps"]["digest_kinds"] = ["PARSER_MISS", "BOGUS"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="BOGUS"):
        load_config(path)


@pytest.mark.parametrize("repo", ["makubexD/mk-invoice-bot", "a/b", "org-1/repo.name_2"])
def test_gaps_github_repo_in_owner_name_form_loads(repo: str) -> None:
    """R-11.5: ``gaps.github.repo`` accepts a GitHub repository written as owner/name."""
    github = GapsGithub.model_validate(
        {
            "enabled": True,
            "repo": repo,
            "app_id": 0,
            "installation_id": 0,
            "labels": ["gap"],
            "daily_write_budget": 20,
        }
    )

    assert github.repo == repo


@pytest.mark.parametrize(
    "repo",
    [
        "mk-invoice-bot",
        "a/b/c",
        "a/",
        "/b",
        "a b/c",
        "https://github.com/a/b",
        "-a/b",
        "a/.",
        "a/..",
    ],
)
def test_gaps_github_repo_that_is_not_owner_name_fails(repo: str) -> None:
    """R-11.5: anything but ``owner/name`` is refused before an issue URL is built from it."""
    with pytest.raises(ValidationError):
        GapsGithub.model_validate(
            {
                "enabled": True,
                "repo": repo,
                "app_id": 0,
                "installation_id": 0,
                "labels": ["gap"],
                "daily_write_budget": 20,
            }
        )


def test_flow_without_reimbursement_loads_with_none() -> None:
    """D53: a flow that does not reimburse carries no cap."""
    cfg = load_config(EXAMPLE)

    assert cfg.flows["personal_google"].reimbursement is None


def test_mail_send_enabled_outside_production_fails(tmp_path: Path) -> None:
    """R-7.7: real mail needs production, so development refuses send_enabled."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["mail"]["send_enabled"] = True
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="send_enabled"):
        load_config(path)


def test_mail_send_enabled_in_production_loads(tmp_path: Path) -> None:
    """R-7.7: production is the one environment where real mail may be enabled."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["env"] = "production"
    document["mail"]["send_enabled"] = True
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).mail.send_enabled is True


def test_inbox_rule_without_owner_work_address_fails_naming_flow(tmp_path: Path) -> None:
    """R-7.7: inbox_rule needs the owner's work mailbox, so a flow listing it without one fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["sinks"][2]["modes"] = ["inbox_rule"]
    del document["mail"]["owner_work_address"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="work_microsoft"):
        load_config(path)


def test_laptop_outlook_without_a_personal_onedrive_sink_fails_naming_flow(tmp_path: Path) -> None:
    """R-7.14: laptop_outlook needs a personal OneDrive sink, so a flow without one fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["flows"]["work_microsoft"]["sinks"][0]
    document["flows"]["work_microsoft"]["sinks"][1]["modes"] = ["laptop_outlook"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="work_microsoft"):
        load_config(path)


def test_mail_delays_default_to_24h_and_4h(tmp_path: Path) -> None:
    """R-7.8: without explicit delays a mail waits 24 h and a laptop job is stale after 4 h."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["mail"]["confirm_timeout"]
    del document["mail"]["agent_stale_after"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    assert cfg.mail.confirm_timeout == timedelta(hours=24)
    assert cfg.mail.agent_stale_after == timedelta(hours=4)


def test_owner_work_address_that_is_not_an_address_fails(tmp_path: Path) -> None:
    """R-7.7: the owner's work mailbox is one plain address, so a mistyped one fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["mail"]["owner_work_address"] = "not-an-address"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="owner_work_address"):
        load_config(path)


def test_xlsm_template_without_allow_macros_fails(tmp_path: Path) -> None:
    """R-7.10: a macro-enabled template needs an explicit opt-in."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["sinks"][1]["template"] = "/data/templates/reembolsos.xlsm"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="allow_macros"):
        load_config(path)


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"modes": ["on_command"]}, "modes"),
        ({"modes": ["graph", "graph"]}, "unique"),
        ({"to": []}, "to"),
    ],
)
def test_mailer_sink_rejects_bad_modes_and_recipients(
    tmp_path: Path, override: dict[str, Any], match: str
) -> None:
    """R-7.7: a mail mode is known and unique, and an invoice mail names a recipient."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["sinks"][2].update(override)
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=match):
        load_config(path)


def test_excel_cell_key_that_is_not_a_reference_fails(tmp_path: Path) -> None:
    """R-7.10: a cell key is A1-style, so a reversed key fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["sinks"][1]["cells"] = {"16B": "{total}"}
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="16B"):
        load_config(path)


def test_zero_reimbursement_cap_fails(tmp_path: Path) -> None:
    """R-7.13: a reimbursement cap is positive, so a zero cap fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["reimbursement"]["cap"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="reimbursement"):
        load_config(path)


def test_reimbursement_track_payments_true_loads(tmp_path: Path) -> None:
    """R-7.12: a flow opts into /pagado payment tracking with track_payments."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["reimbursement"] = {"cap": 50, "track_payments": True}
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).flows["work_microsoft"].reimbursement.track_payments is True


def test_reimbursement_track_payments_defaults_false(tmp_path: Path) -> None:
    """R-7.12: a reimbursement without track_payments does not track payments."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["reimbursement"] = {"cap": 50}
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).flows["work_microsoft"].reimbursement.track_payments is False


def test_reimbursement_track_payments_unknown_key_fails(tmp_path: Path) -> None:
    """R-7.12: reimbursement settings are a closed set, so an unknown key fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["work_microsoft"]["reimbursement"]["track_payment"] = True
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="track_payment"):
        load_config(path)


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


def test_missing_portal_timeout_fails(tmp_path: Path) -> None:
    """R-5.8: retrieval.portal_timeout_s is required, so no default silently sizes a fetch."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["retrieval"]["portal_timeout_s"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "retrieval.portal_timeout_s" in str(excinfo.value)


def test_zero_portal_timeout_fails(tmp_path: Path) -> None:
    """R-5.8: retrieval.portal_timeout_s must be a positive timeout."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["retrieval"]["portal_timeout_s"] = 0
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "retrieval.portal_timeout_s" in str(excinfo.value)


@pytest.mark.parametrize("timeout_s", [120, 270])
def test_portal_timeout_under_job_lease_is_valid(tmp_path: Path, timeout_s: float) -> None:
    """R-5.1: a portal fetch ending at least the margin before the lease loads."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["retrieval"]["portal_timeout_s"] = timeout_s
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).retrieval.portal_timeout_s == timeout_s


@pytest.mark.parametrize("timeout_s", [271, 300, 900])
def test_portal_timeout_at_or_over_job_lease_fails(tmp_path: Path, timeout_s: float) -> None:
    """R-5.1: a fetch must end at least the 30s margin before the 300s lease, so it fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["retrieval"]["portal_timeout_s"] = timeout_s
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "300" in str(excinfo.value)
    assert "margin (30s)" in str(excinfo.value)
    assert str(timeout_s) in str(excinfo.value)


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


def test_ocr_engine_other_than_tesseract_fails(tmp_path: Path) -> None:
    """D81: the helper only runs Tesseract, so another engine fails at load instead of being ignored."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["ocr"]["engine"] = "paddle"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.ocr.engine" in str(excinfo.value)


@pytest.mark.parametrize("lang", ["../x", "-psm"])
def test_ocr_lang_outside_tesseract_codes_fails(tmp_path: Path, lang: str) -> None:
    """D81: an OCR lang that is a path or a flag fails at load before it reaches the argument list."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["ocr"]["lang"] = lang
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "extraction.ocr.lang" in str(excinfo.value)


@pytest.mark.parametrize("lang", ["spa", "spa+eng"])
def test_ocr_lang_tesseract_codes_load(tmp_path: Path, lang: str) -> None:
    """D81: a Tesseract language code loads, alone or combined with another code."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["ocr"]["lang"] = lang
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).extraction.ocr.lang == lang


def test_ocr_timeout_equal_to_helper_timeout_fails(tmp_path: Path) -> None:
    """D81: OCR equal to the helper timeout would be killed by it, so the load fails."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["ocr"]["timeout_s"] = 30
    document["extraction"]["helper_timeout_s"] = 30
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=r"ocr\.timeout_s"):
        load_config(path)


def test_ocr_timeout_below_helper_timeout_loads(tmp_path: Path) -> None:
    """D81: an OCR timeout below the helper timeout leaves OCR time to finish."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["extraction"]["ocr"]["timeout_s"] = 30
    document["extraction"]["helper_timeout_s"] = 31
    path = _write_yaml(tmp_path / "example.yaml", document)

    cfg = load_config(path)

    assert cfg.extraction.ocr.timeout_s == 30
    assert cfg.extraction.helper_timeout_s == 31


def test_repo_directory_loads_and_la_lena_store_resolves_portal() -> None:
    """R-5.8: the repo directory loads and La Leña's store resolves to its portal."""
    directory = load_directory(CONFIG_DIR)

    # The store's RUC lives only in config/stores/ (D47), so find the store by its brand.
    (store,) = [s for s in directory.stores.values() if s.razon_social.startswith("A C INVERSIONES")]
    assert store.portal is not None
    portal = directory.portals[store.portal]
    assert portal.key == "facturactiva-la-lena"
    assert isinstance(portal.fields["#tipoComprobante"], PickField)
    assert isinstance(portal.fields["#fecha"], CalendarField)
    assert portal.tipo_labels["01"] == "Factura"


def test_field_mapping_with_pick_and_select_fails() -> None:
    """R-5.8: each field is one kind, so a mapping with both pick and select is rejected."""
    with pytest.raises(ValidationError):
        Portal.model_validate(
            {
                "key": "example",
                "adapter": "generic_form",
                "fields": {"#tipoComprobante": {"pick": ".option", "select": "{tipo}"}},
            }
        )


def test_portal_without_status_is_todo() -> None:
    """R-5.8: a portal without status is todo, so a half-written file is never live."""
    portal = Portal.model_validate({"key": "example", "adapter": "generic_form"})

    assert portal.status == "todo"


def test_store_ruc_differing_from_file_name_fails(tmp_path: Path) -> None:
    """R-5.8: a store file is named after its RUC, so a mismatch fails naming the file."""
    config_dir = tmp_path / "config"
    _write_store(config_dir, "20000000002", _store_document())

    with pytest.raises(ValueError, match=r"20000000002\.yaml"):
        load_directory(config_dir)


def test_portal_without_retry_schedule_gets_the_default(tmp_path: Path) -> None:
    """D65: a portal without retry_schedule retries at 1h, 6h, 24h and 72h."""
    config_dir = tmp_path / "config"
    _write_portal(config_dir, "example", {"adapter": "generic_form"})

    portal = load_directory(config_dir).portals["example"]

    assert portal.retry_schedule == (
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
        timedelta(hours=72),
    )


def test_portal_retry_schedule_survives_a_json_round_trip() -> None:
    """D65: a portal dumped to JSON (the browser helper's request) validates back unchanged."""
    portal = Portal(key="synthetic", adapter="generic_form", retry_schedule=["90m", "6h", "24h", "2d"])

    restored = Portal.model_validate_json(portal.model_dump_json())

    assert restored == portal
    assert portal.model_dump()["retry_schedule"] == ("90m", "6h", "1d", "2d")


def test_parse_delay_minutes() -> None:
    """D65: a schedule entry such as 90m parses to a timedelta."""
    assert parse_delay("90m") == timedelta(minutes=90)


def test_parse_delay_rejects_weeks() -> None:
    """D65: an unsupported unit raises ValueError naming the text."""
    with pytest.raises(ValueError, match="2w"):
        parse_delay("2w")


def test_retry_schedule_with_an_unknown_unit_fails() -> None:
    """D65: a retry delay only uses minutes, hours or days."""
    with pytest.raises(ValidationError, match="2w"):
        Portal.model_validate({"key": "example", "adapter": "generic_form", "retry_schedule": ["2w"]})


def test_email_inbox_retry_schedule_parses_and_dumps_as_delays() -> None:
    """D65: an email inbox retry schedule is typed and dumps back as canonical delay text."""
    inbox = FlowInbox(provider="gmail", account="invoices_a", retry_schedule=["5m", "1h", "6h", "24h"])

    assert inbox.retry_schedule == (
        timedelta(minutes=5),
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
    )
    assert inbox.model_dump()["retry_schedule"] == ("5m", "1h", "6h", "1d")


def test_email_inbox_retry_schedule_with_an_unknown_unit_fails() -> None:
    """D65: an email inbox retry delay only uses minutes, hours or days."""
    with pytest.raises(ValidationError, match="5 minutes"):
        FlowInbox(provider="gmail", account="invoices_a", retry_schedule=["5 minutes"])


def test_email_inbox_retry_schedule_may_be_empty() -> None:
    """D65: an empty email inbox retry schedule is an empty tuple, as for a portal."""
    inbox = FlowInbox(provider="gmail", account="invoices_a", retry_schedule=[])

    assert inbox.retry_schedule == ()


def test_email_inbox_retry_schedule_in_the_example_config_loads() -> None:
    """R-9.6: the shipped example config's email inbox validates with the typed schedule."""
    cfg = load_config(EXAMPLE)

    assert cfg.flows["personal_google"].email_inbox is not None
    assert cfg.flows["personal_google"].email_inbox.retry_schedule == (
        timedelta(minutes=5),
        timedelta(hours=1),
        timedelta(hours=6),
        timedelta(hours=24),
    )


def test_example_config_has_per_flow_inbox_settings() -> None:
    """D98, R-5.2: the example config loads with the inbox settings under each flow.

    ``config/config.yaml`` is the owner's untracked copy, so CI can load only the example.
    """
    cfg = load_config(EXAMPLE)

    personal = cfg.flows["personal_google"].email_inbox
    work = cfg.flows["work_microsoft"].email_inbox
    assert personal is not None
    assert (personal.provider, personal.account) == ("gmail", "invoices_a")
    assert work is not None
    assert (work.provider, work.account) == ("graph", "bot")


def test_flow_listing_email_inbox_without_settings_fails_naming_the_flow(tmp_path: Path) -> None:
    """D98, R-5.2: listing the email_inbox fetcher without settings fails naming the flow."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["flows"]["personal_google"]["email_inbox"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError) as excinfo:
        load_config(path)

    assert "personal_google" in str(excinfo.value)
    assert "email_inbox" in str(excinfo.value)


def test_retrieval_refuses_email_inbox_settings(tmp_path: Path) -> None:
    """D98, R-5.2: inbox settings belong to the flow, so retrieval refuses them."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["retrieval"]["email_inbox"] = {
        "provider": "gmail",
        "account": "invoices_a",
        "retry_schedule": ["5m"],
    }
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=r"retrieval\.email_inbox"):
        load_config(path)


def test_email_inbox_account_with_an_invalid_name_fails() -> None:
    """D98, R-5.2: an inbox account is lowercase letters, digits and underscores only."""
    with pytest.raises(ValidationError, match="account"):
        FlowInbox(provider="gmail", account="Bad-Name", retry_schedule=["5m"])


_RETRY_SCHEDULE_MODELS = [
    pytest.param(
        lambda schedule: FlowInbox.model_validate(
            {"provider": "gmail", "account": "invoices_a", "retry_schedule": schedule}
        ),
        id="email-inbox",
    ),
    pytest.param(
        lambda schedule: Portal.model_validate(
            {"key": "synthetic", "adapter": "generic_form", "retry_schedule": schedule}
        ),
        id="portal",
    ),
]


@pytest.mark.parametrize("model", _RETRY_SCHEDULE_MODELS)
def test_retry_schedule_parses_and_dumps_the_same_delay_text(
    model: Callable[[object], FlowInbox | Portal],
) -> None:
    """R-9.6: a retry schedule parses to timedeltas and dumps back as the same text (D65)."""
    instance = model(["5m", "1h"])

    assert instance.retry_schedule == (timedelta(minutes=5), timedelta(hours=1))
    assert instance.model_dump()["retry_schedule"] == ("5m", "1h")


@pytest.mark.parametrize("model", _RETRY_SCHEDULE_MODELS)
def test_retry_schedule_with_a_non_string_entry_fails(
    model: Callable[[object], FlowInbox | Portal],
) -> None:
    """R-9.6: a non-string delay is a ValidationError, not a TypeError (D65)."""
    with pytest.raises(ValidationError, match="must be a string"):
        model([5])


@pytest.mark.parametrize("model", _RETRY_SCHEDULE_MODELS)
def test_retry_schedule_with_an_unknown_unit_is_a_validation_error(
    model: Callable[[object], FlowInbox | Portal],
) -> None:
    """R-9.6: a delay outside ``<number><m|h|d>`` fails naming the text (D65)."""
    with pytest.raises(ValidationError, match="5x"):
        model(["5x"])


def test_file_with_a_list_at_the_root_fails(tmp_path: Path) -> None:
    """R-5.8: a directory file is one mapping, so a list root fails naming the file."""
    config_dir = tmp_path / "config"
    stores = config_dir / "stores"
    stores.mkdir(parents=True)
    _write_yaml(stores / f"{SYNTHETIC_RUC}.yaml", [_store_document()])

    with pytest.raises(ValueError, match=rf"{SYNTHETIC_RUC}\.yaml"):
        load_directory(config_dir)


def test_portal_file_setting_key_fails(tmp_path: Path) -> None:
    """R-5.8: a portal's key is its file name, so a key inside the file fails naming the file."""
    config_dir = tmp_path / "config"
    _write_portal(config_dir, "example", {"key": "example", "adapter": "generic_form"})

    with pytest.raises(ValueError, match=r"example\.yaml"):
        load_directory(config_dir)


def _whatsapp_channel(**overrides: Any) -> WhatsAppChannel:
    """Build the WhatsApp channel config the example file carries."""
    fields: dict[str, Any] = {
        "kind": "whatsapp_cloud",
        "phone_number_id": "123456789",
        "graph_api_version": "v23.0",
        "templates": ChannelTemplates(
            completion="invoice_done",
            manual_required="manual_required",
            resume_notice="resume_notice",
            pending_user_reminder="pending_user_reminder",
            batch_preview="batch_preview",
            admin_alert="admin_alert",
        ),
    }
    fields.update(overrides)
    return WhatsAppChannel(**fields)


def test_whatsapp_template_language_rejects_a_dashed_locale() -> None:
    """R-9.6: Meta writes the locale as es_PE, so a dashed tag fails validation."""
    with pytest.raises(ValidationError, match="template_language"):
        _whatsapp_channel(template_language="es-PE")


def test_whatsapp_template_language_defaults_to_es_pe() -> None:
    """R-9.6: a channel with no template_language sends templates in Spanish (Peru)."""
    assert _whatsapp_channel().template_language == "es_PE"


def _config_with_channel(tmp_path: Path, *channels: dict[str, Any], env: str | None = None) -> Path:
    """Write the example config with ``channels`` appended."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["channels"].extend(channels)
    if env is not None:
        document["env"] = env
    return _write_yaml(tmp_path / "example.yaml", document)


def test_console_channel_with_a_known_profile_loads(tmp_path: Path) -> None:
    """D165: a console channel speaking as an existing profile loads in development."""
    cfg = load_config(_config_with_channel(tmp_path, {"kind": "console", "profile": "yo"}))

    assert any(isinstance(channel, ConsoleChannel) for channel in cfg.channels)


def test_console_channel_with_an_unknown_profile_fails(tmp_path: Path) -> None:
    """D165: a console channel speaking as a profile not in profiles is refused, naming it."""
    path = _config_with_channel(tmp_path, {"kind": "console", "profile": "nadie"})

    with pytest.raises(ValidationError, match="nadie"):
        load_config(path)


def test_console_channel_in_production_fails(tmp_path: Path) -> None:
    """D165: the console channel is dev-only, so env production with one is refused."""
    path = _config_with_channel(tmp_path, {"kind": "console", "profile": "yo"}, env="production")

    with pytest.raises(ValidationError, match="D165"):
        load_config(path)


def test_two_console_channels_fail(tmp_path: Path) -> None:
    """D165: only one console channel may be configured."""
    path = _config_with_channel(
        tmp_path,
        {"kind": "console", "profile": "yo"},
        {"kind": "console", "profile": "asistente"},
    )

    with pytest.raises(ValidationError, match="at most one console channel"):
        load_config(path)


def _config_with_profile(tmp_path: Path, profile_id: str) -> Path:
    """Write the example config with its profiles replaced by one keyed by ``profile_id``."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["profiles"] = {profile_id: document["profiles"]["yo"]}
    return _write_yaml(tmp_path / "example.yaml", document)


@pytest.mark.parametrize("profile_id", ["owner", "maku_2"])
def test_config_with_valid_profile_id_loads(tmp_path: Path, profile_id: str) -> None:
    """D93: a profile id of lowercase letters, digits, ``_`` or ``-`` is the natural key."""
    cfg = load_config(_config_with_profile(tmp_path, profile_id))

    assert list(cfg.profiles) == [profile_id]


@pytest.mark.parametrize("profile_id", ["a|b", "Ana", ""])
def test_config_with_invalid_profile_id_fails(tmp_path: Path, profile_id: str) -> None:
    """D93: a profile id outside the natural-key pattern fails at load."""
    with pytest.raises(ValidationError, match="profiles"):
        load_config(_config_with_profile(tmp_path, profile_id))


def test_example_config_declares_each_flows_manual_uploads() -> None:
    """R-1.5: each flow declares the file kinds a sender may upload, in preference order."""
    cfg = load_config(EXAMPLE)

    assert cfg.flows["personal_google"].manual_uploads == ["pdf", "xml"]
    assert cfg.flows["work_microsoft"].manual_uploads == ["pdf", "xml"]


def test_flow_with_an_unsupported_manual_upload_fails(tmp_path: Path) -> None:
    """R-1.5: a sender may upload only a PDF or an XML, so a spreadsheet is refused at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["manual_uploads"] = ["xlsx"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match="manual_uploads"):
        load_config(path)


def test_flow_without_manual_uploads_defaults_to_empty(tmp_path: Path) -> None:
    """R-1.5: a flow that names no manual uploads accepts none by default."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["flows"]["personal_google"]["manual_uploads"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).flows["personal_google"].manual_uploads == []


def test_example_config_has_the_owners_dni() -> None:
    """D143: the shipped example config carries the synthetic owner DNI."""
    cfg = load_config(EXAMPLE)

    assert cfg.owner.dni == "40000000"


def test_owner_without_a_dni_loads_as_none(tmp_path: Path) -> None:
    """D143: owner.dni is optional, so a config without it loads with None."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["owner"]["dni"]
    rules = document["routing"]["rules"]
    document["routing"]["rules"] = [rule for rule in rules if rule["id"] != "BR-R10"]
    path = _write_yaml(tmp_path / "example.yaml", document)

    assert load_config(path).owner.dni is None


def test_owner_dni_with_seven_digits_fails(tmp_path: Path) -> None:
    """D143: the owner's DNI is eight digits, so a seven-digit value fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["owner"]["dni"] = "4000000"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=r"owner\.dni must be 8 digits"):
        load_config(path)


def test_owner_dni_with_a_letter_fails(tmp_path: Path) -> None:
    """D143: the owner's DNI is digits only, so a letter anywhere fails at load."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["owner"]["dni"] = "4000000A"
    path = _write_yaml(tmp_path / "example.yaml", document)

    with pytest.raises(ValidationError, match=r"owner\.dni must be 8 digits"):
        load_config(path)


def test_padron_refresh_cron_parses_time_and_sunday() -> None:
    """S12.4: "0 4 * * 0" is Sunday 04:00 in Python's Monday-0 weekday."""
    padron = Padron(refresh_cron="0 4 * * 0")

    assert padron.at == time(4, 0)
    assert padron.weekday == 6


@pytest.mark.parametrize("refresh_cron", ["*/5 * * * *", "0 4 1 * *", "0 25 * * 0"])
def test_padron_refresh_cron_outside_m_h_star_star_d_fails(refresh_cron: str) -> None:
    """S12.4: a step, a day-of-month and an out-of-range hour are refused, naming the value."""
    with pytest.raises(ValidationError) as excinfo:
        Padron(refresh_cron=refresh_cron)

    assert refresh_cron in str(excinfo.value)


def test_padron_takes_the_default_url_and_size_cap() -> None:
    """S12.4: a padrón config without url or max_mb takes the SUNAT URL and 600 MiB cap."""
    padron = Padron(refresh_cron="0 4 * * 0")

    assert str(padron.url) == "https://www2.sunat.gob.pe/padron_reducido_ruc.zip"
    assert padron.max_mb == 600


def test_padron_takes_the_default_import_deadline() -> None:
    """S12.4: a padrón config without import_timeout_s takes the 1800 s import deadline."""
    padron = Padron(refresh_cron="0 4 * * 0")

    assert padron.import_timeout_s == 1800


def test_padron_url_over_plain_http_is_rejected() -> None:
    """S12.4: a padrón URL that is not https fails validation."""
    with pytest.raises(ValidationError, match=r"padron\.url must use https"):
        Padron(refresh_cron="0 4 * * 0", url="http://example.test/padron.zip")
