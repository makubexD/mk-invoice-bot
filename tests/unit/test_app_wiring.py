"""Unit tests for the app wiring (R-9.11, D119, D124)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import time
from pathlib import Path
from typing import Any, cast

import pyrage
import pytest
import yaml

from invoicebot.app import _daily_tasks, _validation_handlers, _validation_secrets
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.steps import SUNAT_RECHECK_STEP
from invoicebot.pipeline.sunat_recheck import skip_recheck
from invoicebot.pipeline.validate import VALIDATE_STEP
from invoicebot.secrets import Secrets, SecretStore
from invoicebot.sunat import token as sunat

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"


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


def _with_backup_at(cfg: Config, at: time) -> Config:
    """Return ``cfg`` with the backup moved to ``at``."""
    backup = cfg.ops.backup.model_copy(update={"at": at})
    return cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})


def test_daily_tasks_have_the_three_keys_at_their_times() -> None:
    """R-9.11: backup runs at its configured time, retention 30 min later and digest at digest_at."""
    cfg = load_config(EXAMPLE)

    tasks = _daily_tasks(cfg, _noop, _noop, _noop)

    assert {task.key for task in tasks} == {"backup", "retention", "digest"}
    by_key = {task.key: task.at for task in tasks}
    assert by_key["backup"] == cfg.ops.backup.at == time(3, 0)
    assert by_key["retention"] == time(3, 30)
    assert by_key["digest"] == cfg.gaps.digest_at == time(8, 0)


def test_daily_tasks_retention_wraps_past_midnight() -> None:
    """R-9.11: a 23:45 backup wraps retention to 00:15 rather than staying past midnight."""
    cfg = _with_backup_at(load_config(EXAMPLE), time(23, 45))

    tasks = _daily_tasks(cfg, _noop, _noop, _noop)

    assert {task.key: task.at for task in tasks}["retention"] == time(0, 15)


def test_daily_tasks_with_another_backup_target_kind_raise() -> None:
    """D119: a backup target kind other than onedrive fails startup naming the kind."""
    cfg = load_config(EXAMPLE)
    target = cfg.ops.backup.target.model_copy(update={"kind": "s3"})
    backup = cfg.ops.backup.model_copy(update={"target": target})
    unsupported = cfg.model_copy(update={"ops": cfg.ops.model_copy(update={"backup": backup})})

    with pytest.raises(ValueError, match="s3"):
        _daily_tasks(unsupported, _noop, _noop, _noop)


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
