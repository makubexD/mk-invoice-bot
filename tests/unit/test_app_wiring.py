"""Unit tests for the app wiring (R-9.11, D119, D124)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Any, cast

import pyrage
import pytest
import yaml

from invoicebot.app import (
    GOOGLE_CLIENT_SECRET,
    _daily_tasks,
    _fetch_schedules,
    _google_deliver_handler,
    _google_secrets,
    _google_tokens,
    _sol_fetcher,
    _validation_handlers,
    _validation_secrets,
)
from invoicebot.auth.google_oauth import GOOGLE_OAUTH_ADAPTER
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, StoreDirectory, SunatSolSite
from invoicebot.fetchers.sunat_sol import SunatSolFetcher
from invoicebot.pipeline.steps import SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import skip_recheck
from invoicebot.pipeline.validate import VALIDATE_STEP
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.settings import Settings
from invoicebot.sunat import token as sunat
from invoicebot.sunat_origins import SUNAT_SOL

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
SOL_SITE = ROOT / "config" / "sunat_sol.yaml"


async def _noop() -> None:
    """A daily task that does nothing, so these tests need no database."""


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


def _with_backup_at(cfg: Config, at: time) -> Config:
    """Return ``cfg`` with the backup moved to ``at``."""
    backup = cfg.ops.backup.model_copy(update={"at": at})
    return cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})


def _without_google(tmp_path: Path) -> Config:
    """Load the example config with no Google sink, gmail inbox or google block (R-6.4)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    del document["google"]
    flow = document["flows"]["personal_google"]
    flow["sinks"] = []
    flow["email_inbox"]["provider"] = "graph"
    path = tmp_path / "no-google.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def test_daily_tasks_have_the_three_keys_at_their_times() -> None:
    """R-9.11: backup runs at its configured time, retention 30 min later and digest at digest_at."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _noop, _noop, _noop, None)

    assert {task.key for task in tasks} == {"backup", "retention", "digest"}
    by_key = {task.key: task.at for task in tasks}
    assert by_key["backup"] == cfg.ops.backup.at == time(3, 0)
    assert by_key["retention"] == time(3, 30)
    assert by_key["digest"] == cfg.gaps.digest_at == time(8, 0)


def test_daily_tasks_retention_wraps_past_midnight() -> None:
    """R-9.11: a 23:45 backup wraps retention to 00:15 rather than staying past midnight."""
    cfg = _with_backup_at(load_config(EXAMPLE), time(23, 45))

    tasks = _daily_tasks(cfg, _noop, _noop, _noop, None)

    assert {task.key: task.at for task in tasks}["retention"] == time(0, 15)


def test_daily_tasks_with_another_backup_target_kind_raise() -> None:
    """D119: a backup target kind other than onedrive fails startup naming the kind."""
    cfg = load_config(EXAMPLE)
    target = cfg.ops.backup.target.model_copy(update={"kind": "s3"})
    backup = cfg.ops.backup.model_copy(update={"target": target})
    unsupported = cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})

    with pytest.raises(ValueError, match="s3"):
        _daily_tasks(unsupported, _noop, _noop, _noop, None)


def test_daily_tasks_with_github_health_add_it_at_the_digest_time() -> None:
    """BR-D05: with GitHub configured the daily list checks the App at the digest time."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _noop, _noop, _noop, _noop)

    by_key = {task.key: task.at for task in tasks}
    assert by_key["github_health"] == cfg.gaps.digest_at


def test_daily_tasks_without_github_health_add_no_check() -> None:
    """BR-D05: with GitHub disabled the daily list has no GitHub App health check."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _noop, _noop, _noop, None)

    assert "github_health" not in {task.key for task in tasks}


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

    view = _google_secrets(store)

    assert store.scoped_calls == [(GOOGLE_OAUTH_ADAPTER, frozenset({GOOGLE_CLIENT_SECRET}))]
    assert view.missing == frozenset({GOOGLE_CLIENT_SECRET})
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


def test_config_with_google_and_no_secret_fails_startup(tmp_path: Path) -> None:
    """R-6.4: google config without google_client_secret stops startup naming the key."""
    cfg = load_config(EXAMPLE)
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    with pytest.raises(ValueError, match=GOOGLE_CLIENT_SECRET):
        _google_tokens(Settings(data_dir=tmp_path, age_identity_path=None), cfg, cast(Any, None), store)


def test_todo_sol_site_registers_no_fetcher() -> None:
    """R-5.2: a todo SOL site leaves the sunat_sol fetcher unregistered."""
    cfg = load_config(EXAMPLE)

    assert _sol_fetcher(cfg, _sol_directory("todo"), _sol_store("20000000010")) is None


def test_active_sol_site_with_matching_secrets_registers_the_fetcher() -> None:
    """R-5.2: an active site with matching SOL secrets builds the fetcher."""
    cfg = _with_owner_ruc(load_config(EXAMPLE), "20000000010")

    fetcher = _sol_fetcher(cfg, _sol_directory("active"), _sol_store("20000000010"))

    assert isinstance(fetcher, SunatSolFetcher)


def test_mismatched_sol_ruc_stops_startup_naming_only_the_key() -> None:
    """R-9.10: a sol_ruc that is not the owner's RUC fails startup without either value."""
    cfg = _with_owner_ruc(load_config(EXAMPLE), "20000000010")

    with pytest.raises(ValueError, match="sol_ruc") as raised:
        _sol_fetcher(cfg, _sol_directory("active"), _sol_store("20000000028"))

    assert "20000000010" not in str(raised.value)
    assert "20000000028" not in str(raised.value)


def test_sunat_sol_schedule_comes_from_the_site_file() -> None:
    """R-5.2: sunat_sol's retry schedule is the site query's schedule."""
    directory = _sol_directory("todo")

    schedule = _fetch_schedules(directory)[SUNAT_SOL]

    assert directory.sunat_sol is not None
    assert schedule(cast(Any, None), cast(Any, None)) == directory.sunat_sol.query.retry_schedule
