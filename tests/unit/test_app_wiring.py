"""Unit tests for the app wiring (R-9.11, D119, D124)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import time
from pathlib import Path
from typing import Any, cast

import httpx
import pyrage
import pytest
import yaml

from invoicebot.app import (
    GITHUB_APP_PRIVATE_KEY,
    GOOGLE_CLIENT_SECRET,
    LAPTOP_ADAPTER,
    LAPTOP_AGENT_KEY,
    DailyRuns,
    _channel_builders,
    _check_reload_candidate,
    _daily_tasks,
    _deliver_handlers,
    _deliver_registry,
    _fetch_schedules,
    _github_health_run,
    _github_secrets,
    _google_deliver_handler,
    _google_tokens,
    _laptop_secrets,
    _log_reload_failure,
    _secret_problem,
    _sol_fetcher,
    _sol_secrets,
    _validation_handlers,
    _validation_secrets,
)
from invoicebot.auth.google_oauth import GOOGLE_OAUTH_ADAPTER, google_secrets
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, ConsoleChannel, StoreDirectory, SunatSolSite
from invoicebot.fetchers.sunat_sol import SECRETS, SunatSolFetcher
from invoicebot.pipeline.steps import DELIVER_STEP, SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import skip_recheck
from invoicebot.pipeline.validate import VALIDATE_STEP
from invoicebot.runtime_config import ConfigRejected
from invoicebot.sandbox import HelperPrefixes
from invoicebot.secrets import SecretNotDeclared, Secrets, SecretStore
from invoicebot.settings import Settings
from invoicebot.sunat import token as sunat
from invoicebot.sunat_origins import SUNAT_SOL
from invoicebot.wiring.channels import CHANNEL_ADAPTER, CHANNEL_SECRETS, SENDER_HASH_KEY, channel_secrets
from invoicebot.wiring.whatsapp import WHATSAPP_ADAPTER, WHATSAPP_SECRETS, whatsapp_wiring

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
SOL_SITE = ROOT / "config" / "sunat_sol.yaml"


async def _noop() -> None:
    """A daily task that does nothing, so these tests need no database."""


def _runs(github_health: Callable[[], Awaitable[None]] | None = None) -> DailyRuns:
    """Return daily runs that all do nothing, with the GitHub check only when given."""
    return DailyRuns(
        backup=_noop,
        retention=_noop,
        housekeeping=_noop,
        padron=_noop,
        digest=_noop,
        github_health=github_health,
    )


def _sunat_config(tmp_path: Path, *, enabled: bool) -> Config:
    """Load the example config with SUNAT validation switched on or off (D124)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["sunat"]["validez_api"]["enabled"] = enabled
    path = tmp_path / f"sunat-{enabled}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _loaded(tmp_path: Path, values: dict[str, str]) -> SecretStore:
    """Encrypt ``values`` into a test age store and load it back (R-9.9)."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(identity) + "\n", encoding="utf-8")
    store_path = tmp_path / "secrets.age"
    store_path.write_bytes(pyrage.encrypt(yaml.safe_dump(values).encode("utf-8"), [identity.to_public()]))
    return SecretStore.load(store_path, identity_path)


class _RecordingStore(SecretStore):
    """A secret store that records every scoped view it hands out (R-9.10)."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__(values)
        self.scoped_calls: list[tuple[str, frozenset[str]]] = []

    def scoped(self, adapter_name: str, declared: set[str]) -> Secrets:
        """Record the adapter and its declared keys, then return the real view."""
        self.scoped_calls.append((adapter_name, frozenset(declared)))
        return super().scoped(adapter_name, declared)


def _with_owner_ruc(cfg: Config, ruc: str) -> Config:
    """Return ``cfg`` with the owner's RUC replaced (R-9.10)."""
    return cfg.model_copy(update={"owner": cfg.owner.model_copy(update={"ruc": ruc})})


def _sol_directory(status: str) -> StoreDirectory:
    """Return a store directory holding the shipped SOL site with ``status`` (R-5.2)."""
    site = SunatSolSite.model_validate(yaml.safe_load(SOL_SITE.read_text(encoding="utf-8")))
    return StoreDirectory(stores={}, portals={}, sunat_sol=site.model_copy(update={"status": status}))


def _sol_store(sol_ruc: str) -> SecretStore:
    """Return a store with the SOL sign-in secrets, whose RUC is ``sol_ruc`` (R-9.10)."""
    return SecretStore(
        {
            "sol_ruc": sol_ruc,
            "sol_user": "synthetic-user",
            "sol_password": "synthetic-password",
        }
    )


def _base_secrets() -> dict[str, str]:
    """Return synthetic values for the secrets the example config always needs (R-9.6)."""
    return {
        **dict.fromkeys(WHATSAPP_SECRETS, "synthetic-value"),
        **dict.fromkeys(CHANNEL_SECRETS, "synthetic-value"),
        GITHUB_APP_PRIVATE_KEY: "synthetic-pem",
    }


def _with_laptop_mode(tmp_path: Path) -> Config:
    """Load the example config, minus Google, whose mailer also lists the laptop mode (R-7.14)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["google"]
    _drop_dni_flow(document)
    flow = document["flows"]["personal_google"]
    flow["sinks"] = []
    flow["email_inbox"]["provider"] = "graph"
    document["microsoft"]["accounts"][flow["email_inbox"]["account"]] = "invoices@example.com"
    for candidate in document["flows"].values():
        for sink in candidate["sinks"]:
            if sink["kind"] == "mailer":
                sink["modes"] = [*sink["modes"], LAPTOP_ADAPTER]
    path = tmp_path / "laptop.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _with_backup_at(cfg: Config, at: time) -> Config:
    """Return ``cfg`` with the backup moved to ``at``."""
    backup = cfg.ops.backup.model_copy(update={"at": at})
    return cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})


def _drop_dni_flow(document: dict[str, Any]) -> None:
    """Remove the Sheets-only ``personal_dni`` flow and what names it (D144)."""
    del document["flows"]["personal_dni"]
    del document["llm_fallback"]["flows"]["personal_dni"]
    rules = document["routing"]["rules"]
    document["routing"]["rules"] = [rule for rule in rules if rule["flow"] != "personal_dni"]
    for profile in document["profiles"].values():
        profile["flows"] = [name for name in profile["flows"] if name != "personal_dni"]


def _without_google(tmp_path: Path) -> Config:
    """Load the example config with no Google sink, gmail inbox or google block (R-6.4)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["google"]
    _drop_dni_flow(document)
    flow = document["flows"]["personal_google"]
    flow["sinks"] = []
    flow["email_inbox"]["provider"] = "graph"
    document["microsoft"]["accounts"][flow["email_inbox"]["account"]] = "invoices@example.com"
    path = tmp_path / "no-google.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _sheets_only(tmp_path: Path) -> Config:
    """Load the example config with its Google flow keeping only the sheets sink (D144)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    flow = document["flows"]["personal_google"]
    flow["sinks"] = [sink for sink in flow["sinks"] if sink["kind"] != "google_drive"]
    sheets = next(sink for sink in flow["sinks"] if sink["kind"] == "google_sheets")
    sheets["columns"] = {
        name: expression for name, expression in sheets["columns"].items() if "link_" not in expression
    }
    path = tmp_path / "sheets-only.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _without_google_sinks(tmp_path: Path) -> Config:
    """Load the example config with no Google sink, keeping the google block (D144)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"]["personal_google"]["sinks"] = []
    _drop_dni_flow(document)
    path = tmp_path / "no-google-sinks.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def test_daily_tasks_have_their_keys_at_their_times() -> None:
    """R-9.11: backup at 3, retention and housekeeping 30 min later, padrón at 4, digest at 8."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _runs())

    assert {task.key for task in tasks} == {"backup", "retention", "housekeeping", "padron", "digest"}
    by_key = {task.key: task.at for task in tasks}
    assert by_key["backup"] == cfg.ops.backup.at == time(3, 0)
    assert by_key["retention"] == by_key["housekeeping"] == time(3, 30)
    assert by_key["padron"] == cfg.sunat.padron.at == time(4, 0)
    assert by_key["digest"] == cfg.gaps.digest_at == time(8, 0)


def test_daily_tasks_retention_wraps_past_midnight() -> None:
    """R-9.11: a 23:45 backup wraps retention to 00:15 rather than staying past midnight."""
    cfg = _with_backup_at(load_config(EXAMPLE), time(23, 45))

    tasks = _daily_tasks(cfg, _runs())

    assert {task.key: task.at for task in tasks}["retention"] == time(0, 15)


def test_daily_tasks_with_another_backup_target_kind_raise() -> None:
    """D119: a backup target kind other than onedrive fails startup naming the kind."""
    cfg = load_config(EXAMPLE)
    target = cfg.ops.backup.target.model_copy(update={"kind": "s3"})
    backup = cfg.ops.backup.model_copy(update={"target": target})
    unsupported = cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})

    with pytest.raises(ValueError, match="s3"):
        _daily_tasks(unsupported, _runs())


def test_daily_tasks_with_github_health_add_it_at_the_digest_time() -> None:
    """BR-D05: with GitHub configured the daily list checks the App at the digest time."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _runs(github_health=_noop))

    by_key = {task.key: task.at for task in tasks}
    assert by_key["github_health"] == cfg.gaps.digest_at


def test_daily_tasks_without_github_health_add_no_check() -> None:
    """BR-D05: with GitHub disabled the daily list has no GitHub App health check."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _runs())

    assert "github_health" not in {task.key for task in tasks}


def test_github_health_run_without_an_app_is_none() -> None:
    """BR-D05: with no GitHub App there is no daily health check to run."""
    cfg = load_config(EXAMPLE)

    assert _github_health_run(None, cast(Any, None), cfg) is None


def test_github_health_run_with_an_app_is_bound_to_it() -> None:
    """BR-D05: with a GitHub App the daily health check is a call bound to that App."""
    cfg = load_config(EXAMPLE)
    github_app = cast(Any, object())

    run = _github_health_run(github_app, cast(Any, None), cfg)

    assert run is not None
    assert cast(Any, run).args[0] is github_app


def test_sunat_validation_off_reads_no_secret(tmp_path: Path) -> None:
    """D124: with validation off the app never scopes the SUNAT secrets."""
    cfg = _sunat_config(tmp_path, enabled=False)
    store = _RecordingStore({"other_key": "synthetic-other-value"})

    assert _validation_secrets(cfg, store) is None
    assert store.scoped_calls == []


def test_sunat_validation_on_with_missing_secret_fails_closed(tmp_path: Path) -> None:
    """D124: an enabled API without its keys returns a disabled view the runtime refuses."""
    cfg = _sunat_config(tmp_path, enabled=True)
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    secrets = _validation_secrets(cfg, store)

    assert secrets is not None
    assert not secrets.enabled
    assert secrets.missing == set(sunat.SECRETS)


def test_sunat_validation_off_registers_the_skipping_recheck_step(tmp_path: Path) -> None:
    """D125: with validation off the recheck step is registered and only skips, so no recheck job fails."""
    cfg = _sunat_config(tmp_path, enabled=False)

    handlers = _validation_handlers(cfg, cast(Any, None), cast(Any, None), None)

    assert set(handlers) == {VALIDATE_STEP, SUNAT_RECHECK_STEP}
    assert handlers[SUNAT_RECHECK_STEP] is skip_recheck


def test_sunat_validation_on_with_secrets_registers_the_recheck_step(tmp_path: Path) -> None:
    """D124: with both keys present the app registers the validate and recheck steps."""
    cfg = _sunat_config(tmp_path, enabled=True)
    store = _loaded(tmp_path, dict.fromkeys(sunat.SECRETS, "synthetic-value"))

    secrets = _validation_secrets(cfg, store)

    assert secrets is not None
    assert secrets.enabled
    handlers = _validation_handlers(cfg, cast(Any, None), cast(Any, None), secrets)
    assert set(handlers) == {VALIDATE_STEP, SUNAT_RECHECK_STEP}


def test_google_secret_view_declares_only_the_client_secret() -> None:
    """R-9.10: the Google OAuth view declares google_client_secret and nothing else."""
    store = _RecordingStore({"other_key": "synthetic-other-value"})

    view = google_secrets(store)

    assert store.scoped_calls == [(GOOGLE_OAUTH_ADAPTER, frozenset({GOOGLE_CLIENT_SECRET}))]
    assert view.missing == frozenset({GOOGLE_CLIENT_SECRET})
    assert not view.enabled


def test_google_secret_view_is_enabled_when_the_client_secret_is_present() -> None:
    """R-9.10: a Google view holding its client secret is enabled and refuses an undeclared key."""
    store = _RecordingStore(
        {"google_client_secret": "synthetic-client-secret", "other_key": "synthetic-other-value"}
    )

    view = google_secrets(store)

    assert view.enabled
    assert view.missing == frozenset()
    with pytest.raises(SecretNotDeclared):
        view.get("other_key")


@pytest.mark.parametrize(
    ("view", "keys"),
    [
        (_github_secrets, {GITHUB_APP_PRIVATE_KEY}),
        (_laptop_secrets, {LAPTOP_AGENT_KEY}),
        (_sol_secrets, SECRETS),
    ],
)
def test_feature_secret_view_is_enabled_only_with_every_key(
    view: Callable[[SecretStore], Secrets], keys: Iterable[str]
) -> None:
    """R-9.10: each feature's view is enabled with all its keys and disabled without them."""
    assert view(SecretStore(dict.fromkeys(keys, "synthetic-value"))).enabled
    assert not view(SecretStore({})).enabled


def test_whatsapp_secret_view_no_longer_declares_the_sender_hash_key() -> None:
    """R-9.10: the WhatsApp view declares its three keys and not the shared sender hash key."""
    cfg = load_config(EXAMPLE)
    store = _RecordingStore({})

    whatsapp_wiring.secret_problem(cfg, store)

    assert store.scoped_calls[0] == (WHATSAPP_ADAPTER, WHATSAPP_SECRETS)


def test_channel_secret_view_declares_only_the_sender_hash_key() -> None:
    """R-9.10: the shared channel view declares sender_hash_key and nothing else."""
    store = _RecordingStore({"whatsapp_token": "synthetic-token-value"})

    view = channel_secrets(store)

    assert store.scoped_calls == [(CHANNEL_ADAPTER, frozenset({SENDER_HASH_KEY}))]
    assert view.missing == frozenset({SENDER_HASH_KEY})
    assert not view.enabled


def test_config_without_google_builds_no_google_clients(tmp_path: Path) -> None:
    """R-6.4: without google config there are no Google tokens and no flow A deliverer."""
    cfg = _without_google(tmp_path)
    store = _RecordingStore({})

    tokens = _google_tokens(Settings(data_dir=tmp_path), cfg, cast(Any, None), store)
    handler = _google_deliver_handler(cfg, cast(Any, None), cast(Any, None), tokens)

    assert tokens == {}
    assert handler is None
    assert store.scoped_calls == []


def test_sheets_only_google_flow_gets_a_deliverer(tmp_path: Path) -> None:
    """D144: a flow with only a google_sheets sink gets the Google deliver dispatcher."""
    cfg = _sheets_only(tmp_path)

    handlers = _deliver_registry(cfg, None, cast(Any, None), cast(Any, None), {})

    assert DELIVER_STEP in handlers


def test_config_without_google_sinks_builds_no_google_handler(tmp_path: Path) -> None:
    """D144: with no Google sink of either kind no Google deliver handler is built."""
    cfg = _without_google_sinks(tmp_path)

    handler = _google_deliver_handler(cfg, cast(Any, None), cast(Any, None), {})

    assert handler is None


def test_config_with_google_and_no_secret_fails_startup(tmp_path: Path) -> None:
    """R-6.4: google config without google_client_secret stops startup naming the key."""
    cfg = load_config(EXAMPLE)
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    with pytest.raises(ValueError, match=GOOGLE_CLIENT_SECRET):
        _google_tokens(Settings(data_dir=tmp_path, age_identity_path=None), cfg, cast(Any, None), store)


def test_todo_sol_site_registers_no_fetcher() -> None:
    """R-5.2: a todo SOL site leaves the sunat_sol fetcher unregistered."""
    cfg = load_config(EXAMPLE)

    assert (
        _sol_fetcher(cfg, _sol_directory("todo"), _sol_store("20000000010"), HelperPrefixes((), ())) is None
    )


def test_active_sol_site_with_matching_secrets_registers_the_fetcher() -> None:
    """R-5.2: an active site with matching SOL secrets builds the fetcher."""
    cfg = _with_owner_ruc(load_config(EXAMPLE), "20000000010")

    fetcher = _sol_fetcher(cfg, _sol_directory("active"), _sol_store("20000000010"), HelperPrefixes((), ()))

    assert isinstance(fetcher, SunatSolFetcher)


def test_mismatched_sol_ruc_stops_startup_naming_only_the_key() -> None:
    """R-9.10: a sol_ruc that is not the owner's RUC fails startup without either value."""
    cfg = _with_owner_ruc(load_config(EXAMPLE), "20000000010")

    with pytest.raises(ValueError, match="sol_ruc") as raised:
        _sol_fetcher(cfg, _sol_directory("active"), _sol_store("20000000028"), HelperPrefixes((), ()))

    assert "20000000010" not in str(raised.value)
    assert "20000000028" not in str(raised.value)


def test_secret_problem_with_every_needed_secret_is_none(tmp_path: Path) -> None:
    """R-9.6: a candidate whose enabled features have their secrets raises no problem."""
    cfg = _without_google(tmp_path)
    store = SecretStore(_base_secrets())

    assert _secret_problem(cfg, _sol_directory("todo"), store, _channel_builders(tmp_path)) is None


def test_secret_problem_without_the_whatsapp_token_is_whatsapp(tmp_path: Path) -> None:
    """R-9.6: a candidate missing a WhatsApp secret is refused as secrets:whatsapp."""
    cfg = _without_google(tmp_path)
    values = _base_secrets()
    del values["whatsapp_token"]
    store = SecretStore(values)

    assert (
        _secret_problem(cfg, _sol_directory("todo"), store, _channel_builders(tmp_path)) == "secrets:whatsapp"
    )


def test_secret_problem_without_the_sender_hash_key_is_channels(tmp_path: Path) -> None:
    """R-9.10: a chat-enabled candidate without the shared hash key is refused as secrets:channels."""
    cfg = _without_google(tmp_path)
    values = _base_secrets()
    del values[SENDER_HASH_KEY]

    assert (
        _secret_problem(cfg, _sol_directory("todo"), SecretStore(values), _channel_builders(tmp_path))
        == "secrets:channels"
    )


def test_secret_problem_for_a_console_only_candidate_needs_no_channel_secret(tmp_path: Path) -> None:
    """D174, D165: a console-only candidate starts without the shared hash key, so a reload accepts it."""
    cfg = _without_google(tmp_path)
    console = ConsoleChannel(kind="console", profile=cfg.owner.profile)
    cfg = cfg.model_copy(update={"channels": [console]})
    values = _base_secrets()
    del values[SENDER_HASH_KEY]

    assert (
        _secret_problem(cfg, _sol_directory("todo"), SecretStore(values), _channel_builders(tmp_path)) is None
    )


def test_secret_problem_with_a_console_channel_is_not_channels_none(tmp_path: Path) -> None:
    """D183: a console-only candidate has a chat channel, so a reload does not refuse it as channels:none."""
    cfg = _without_google(tmp_path)
    console = ConsoleChannel(kind="console", profile=cfg.owner.profile)
    cfg = cfg.model_copy(update={"channels": [console]})

    assert (
        _secret_problem(
            cfg, _sol_directory("todo"), SecretStore(_base_secrets()), _channel_builders(tmp_path)
        )
        != "channels:none"
    )


def test_secret_problem_with_a_whatsapp_channel_is_not_channels_none(tmp_path: Path) -> None:
    """D183: a WhatsApp-only candidate has a chat channel, so a reload does not refuse it as channels:none."""
    cfg = _without_google(tmp_path)
    channels = [channel for channel in cfg.channels if channel.kind == "whatsapp_cloud"]
    cfg = cfg.model_copy(update={"channels": channels})

    assert (
        _secret_problem(
            cfg, _sol_directory("todo"), SecretStore(_base_secrets()), _channel_builders(tmp_path)
        )
        != "channels:none"
    )


def test_secret_problem_without_any_channel_is_channels_none(tmp_path: Path) -> None:
    """D183: a candidate with no chat channel at all is refused as channels:none."""
    cfg = _without_google(tmp_path).model_copy(update={"channels": []})

    assert (
        _secret_problem(
            cfg, _sol_directory("todo"), SecretStore(_base_secrets()), _channel_builders(tmp_path)
        )
        == "channels:none"
    )


def test_secret_problem_with_validation_on_and_no_keys_is_sunat_validez(tmp_path: Path) -> None:
    """R-9.6: validation switched on without the SUNAT keys is refused as secrets:sunat_validez."""
    cfg = _sunat_config(tmp_path, enabled=True)
    store = SecretStore(_base_secrets())

    assert (
        _secret_problem(cfg, _sol_directory("todo"), store, _channel_builders(tmp_path))
        == "secrets:sunat_validez"
    )


def test_secret_problem_with_google_and_no_client_secret_is_google(tmp_path: Path) -> None:
    """R-9.6: a candidate with google config but no client secret is refused as secrets:google."""
    cfg = load_config(EXAMPLE)
    store = SecretStore(_base_secrets())

    assert (
        _secret_problem(cfg, _sol_directory("todo"), store, _channel_builders(tmp_path)) == "secrets:google"
    )


def test_secret_problem_with_an_active_sol_site_whose_ruc_differs_is_sunat_sol(tmp_path: Path) -> None:
    """R-9.6: an active SOL site signing in as another RUC is refused as secrets:sunat_sol."""
    cfg = _with_owner_ruc(_without_google(tmp_path), "20000000010")
    values = _base_secrets()
    values.update(
        {"sol_ruc": "20000000028", "sol_user": "synthetic-user", "sol_password": "synthetic-password"}
    )
    store = SecretStore(values)

    assert (
        _secret_problem(cfg, _sol_directory("active"), store, _channel_builders(tmp_path))
        == "secrets:sunat_sol"
    )


def test_secret_problem_with_an_active_sol_site_and_no_sol_keys_is_sunat_sol(tmp_path: Path) -> None:
    """R-9.6: an active SOL site without its keys is refused, as startup would be (R-9.10)."""
    cfg = _with_owner_ruc(_without_google(tmp_path), "20000000010")
    store = SecretStore(_base_secrets())

    assert (
        _secret_problem(cfg, _sol_directory("active"), store, _channel_builders(tmp_path))
        == "secrets:sunat_sol"
    )


def test_secret_problem_with_an_active_sol_site_and_matching_keys_is_none(tmp_path: Path) -> None:
    """R-9.6: an active SOL site signing in as the owner raises no problem."""
    cfg = _with_owner_ruc(_without_google(tmp_path), "20000000010")
    values = _base_secrets()
    values.update(
        {"sol_ruc": "20000000010", "sol_user": "synthetic-user", "sol_password": "synthetic-password"}
    )

    assert (
        _secret_problem(cfg, _sol_directory("active"), SecretStore(values), _channel_builders(tmp_path))
        is None
    )


def test_secret_problem_with_github_on_and_no_app_key_is_github(tmp_path: Path) -> None:
    """R-9.6: GitHub gaps switched on without the App's private key is refused as secrets:github."""
    values = _base_secrets()
    del values[GITHUB_APP_PRIVATE_KEY]

    assert _secret_problem(
        _without_google(tmp_path), _sol_directory("todo"), SecretStore(values), _channel_builders(tmp_path)
    ) == ("secrets:github")


def test_secret_problem_with_a_laptop_mode_and_no_agent_key_is_laptop(tmp_path: Path) -> None:
    """R-9.6: a flow listing laptop_outlook without the agent key is refused as secrets:laptop."""
    cfg = _with_laptop_mode(tmp_path)

    assert (
        _secret_problem(
            cfg, _sol_directory("todo"), SecretStore(_base_secrets()), _channel_builders(tmp_path)
        )
        == "secrets:laptop"
    )


def test_secret_problem_with_a_laptop_mode_and_its_agent_key_is_none(tmp_path: Path) -> None:
    """R-9.6: a flow listing laptop_outlook with the agent key raises no problem."""
    cfg = _with_laptop_mode(tmp_path)
    store = SecretStore({**_base_secrets(), LAPTOP_AGENT_KEY: "synthetic-key"})

    assert _secret_problem(cfg, _sol_directory("todo"), store, _channel_builders(tmp_path)) is None


def _reload_settings(tmp_path: Path, plaintext: bytes) -> Settings:
    """Return settings whose age store decrypts to ``plaintext`` (R-9.6)."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(identity) + "\n", encoding="utf-8")
    secrets_path = tmp_path / "secrets.age"
    secrets_path.write_bytes(pyrage.encrypt(plaintext, [identity.to_public()]))
    return Settings(
        config_path=EXAMPLE,
        parsing_rules_path=ROOT / "config" / "parsing_rules.yaml",
        secrets_path=secrets_path,
        age_identity_path=identity_path,
        data_dir=tmp_path,
    )


def test_check_reload_candidate_with_every_secret_returns_the_runtime(tmp_path: Path) -> None:
    """R-9.6: a candidate whose secrets are all present is accepted and returned."""
    values = {**_base_secrets(), GOOGLE_CLIENT_SECRET: "synthetic-secret"}

    runtime = _check_reload_candidate(_reload_settings(tmp_path, yaml.safe_dump(values).encode("utf-8")))

    assert runtime.cfg.owner.ruc == load_config(EXAMPLE).owner.ruc


def test_check_reload_candidate_without_the_google_secret_is_google(tmp_path: Path) -> None:
    """R-9.6: a candidate missing the Google client secret is rejected as secrets:google."""
    plaintext = yaml.safe_dump(_base_secrets()).encode("utf-8")

    with pytest.raises(ConfigRejected) as raised:
        _check_reload_candidate(_reload_settings(tmp_path, plaintext))

    assert raised.value.reason == "secrets:google"


def test_check_reload_candidate_with_malformed_plaintext_hides_it(tmp_path: Path) -> None:
    """R-9.6, R-9.16: a secrets plaintext that is not YAML is rejected without quoting it."""
    plaintext = b"whatsapp_token: synthetic-leak\n  bad: [unclosed\n"

    with pytest.raises(ConfigRejected) as raised:
        _check_reload_candidate(_reload_settings(tmp_path, plaintext))

    assert raised.value.reason == "secrets"
    assert raised.value.__cause__ is not None
    assert "synthetic-leak" not in str(raised.value.__cause__)
    assert raised.value.__cause__.__cause__ is None


def test_check_reload_candidate_without_a_secrets_file_is_rejected(tmp_path: Path) -> None:
    """R-9.6: a candidate whose secrets file is missing is rejected with reason secrets."""
    identity = pyrage.x25519.Identity.generate()
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(identity) + "\n", encoding="utf-8")
    settings = Settings(
        config_path=EXAMPLE,
        parsing_rules_path=ROOT / "config" / "parsing_rules.yaml",
        secrets_path=tmp_path / "missing.age",
        age_identity_path=identity_path,
        data_dir=tmp_path,
    )

    with pytest.raises(ConfigRejected) as raised:
        _check_reload_candidate(settings)

    assert raised.value.reason == "secrets"


def test_sunat_sol_schedule_comes_from_the_site_file() -> None:
    """R-5.2: sunat_sol's retry schedule is the site query's schedule."""
    directory = _sol_directory("todo")

    schedule = _fetch_schedules(directory)[SUNAT_SOL]

    assert directory.sunat_sol is not None
    assert schedule(cast(Any, None), cast(Any, None)) == directory.sunat_sol.query.retry_schedule


async def test_a_reload_task_that_raises_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """R-9.6: a reload that raises instead of rejecting logs one error, so it never fails silently."""

    async def _boom() -> bool:
        raise OSError("gone")

    task = asyncio.create_task(_boom())
    await asyncio.wait([task])

    with caplog.at_level(logging.ERROR, logger="invoicebot.app"):
        _log_reload_failure(task)

    assert [record.getMessage() for record in caplog.records] == ["config reload failed"]


async def test_single_microsoft_account_builds_flow_b_without_the_bot_mailbox(tmp_path: Path) -> None:
    """R-7.1, D164: a config that pins only ``personal`` wires flow B and never asks for ``bot``."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    flow = document["flows"]["work_microsoft"]
    flow["email_inbox"]["account"] = "personal"
    for sink in flow["sinks"]:
        if sink["kind"] == "mailer":
            sink["modes"] = ["graph"]
    document["microsoft"]["accounts"] = {"personal": "personal@example.com"}
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    cfg = load_config(config_path)
    identity_path = tmp_path / "identity.key"
    identity_path.write_text(str(pyrage.x25519.Identity.generate()) + "\n", encoding="utf-8")
    settings = Settings(age_identity_path=identity_path, data_dir=tmp_path)

    async with httpx.AsyncClient() as http:
        handlers = _deliver_handlers(
            settings, cfg, cast(Any, None), http, cast(Any, None), _RecordingStore({})
        )

    assert handlers is not None
