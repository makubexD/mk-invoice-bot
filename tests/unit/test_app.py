"""Unit tests for the app wiring (R-7.12, R-7.14, R-11.10)."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
import pyrage
import pytest
import yaml

from invoicebot.app import (
    GITHUB_ADAPTER,
    GITHUB_APP_PRIVATE_KEY,
    LAPTOP_AGENT_KEY,
    _github_app,
    _github_transports,
    _laptop_root_and_key,
    _payment_flow,
    _teach_setup,
)
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.commands import PaymentFlow
from invoicebot.secrets import SecretMissing, Secrets, SecretStore

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
#: The example flow that carries reimbursement settings.
REIMBURSING_FLOW = "work_microsoft"


def _config(tmp_path: Path, *, track_payments: bool) -> Config:
    """Load the example config with payment tracking set on its reimbursing flow."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["flows"][REIMBURSING_FLOW]["reimbursement"]["track_payments"] = track_payments
    path = tmp_path / "example.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


def _mail_config(tmp_path: Path, mode: str | None) -> Config:
    """Load the example config, listing ``mode`` on the reimbursing flow's mailer when given."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    if mode is not None:
        for sink in document["flows"][REIMBURSING_FLOW]["sinks"]:
            if sink.get("kind") == "mailer":
                sink["modes"] = [mode]
    path = tmp_path / "laptop.yaml"
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


def _github_config(tmp_path: Path, *, enabled: bool) -> Config:
    """Load the example config with the GitHub gap transport switched on or off (R-11.10)."""
    document: dict[str, Any] = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["gaps"]["github"]["enabled"] = enabled
    path = tmp_path / f"github-{enabled}.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return load_config(path)


class _RecordingStore(SecretStore):
    """A secret store that records every scoped view it hands out (R-9.10)."""

    def __init__(self, values: Mapping[str, str]) -> None:
        super().__init__(values)
        self.scoped_calls: list[tuple[str, frozenset[str]]] = []

    def scoped(self, adapter_name: str, declared: set[str]) -> Secrets:
        """Record the adapter and its declared keys, then return the real view."""
        self.scoped_calls.append((adapter_name, frozenset(declared)))
        return super().scoped(adapter_name, declared)


def test_payment_flow_tracking_flow_gives_its_id_and_cap(tmp_path: Path) -> None:
    """R-7.12: the flow that tracks payments supplies its id and cap to commands."""
    cfg = _config(tmp_path, track_payments=True)

    assert _payment_flow(cfg) == PaymentFlow(flow_id=REIMBURSING_FLOW, cap=Decimal("50.00"))


def test_payment_flow_with_no_tracking_flow_is_none(tmp_path: Path) -> None:
    """R-7.12: with no flow tracking payments, payment commands stay off."""
    cfg = _config(tmp_path, track_payments=False)

    assert _payment_flow(cfg) is None


def test_teach_setup_with_chat_teaching_off_is_none() -> None:
    """R-11.4: with gaps.teach_via_chat false there is no teaching setup."""
    cfg = load_config(EXAMPLE)
    off = cfg.model_copy(update={"gaps": cfg.gaps.model_copy(update={"teach_via_chat": False})})

    assert _teach_setup(off) is None


def test_teach_setup_with_chat_teaching_on_carries_the_config_profiles() -> None:
    """R-11.4: with gaps.teach_via_chat true the setup carries the config profiles."""
    cfg = load_config(EXAMPLE)

    setup = _teach_setup(cfg)

    assert setup is not None
    assert setup.profiles == cfg.profiles


def test_laptop_mode_without_the_agent_key_raises_secret_missing(tmp_path: Path) -> None:
    """R-7.14: a flow listing laptop_outlook without the agent key stops startup."""
    cfg = _mail_config(tmp_path, "laptop_outlook")
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    with pytest.raises(SecretMissing):
        _laptop_root_and_key(cfg, store)


def test_laptop_mode_with_the_agent_key_returns_its_root_and_key(tmp_path: Path) -> None:
    """R-7.14: a flow listing laptop_outlook gets its OneDrive root and the agent key."""
    cfg = _mail_config(tmp_path, "laptop_outlook")
    store = _loaded(tmp_path, {LAPTOP_AGENT_KEY: "test-key"})

    found = _laptop_root_and_key(cfg, store)

    assert found is not None
    root, key = found
    assert root
    assert key == b"test-key"


def test_laptop_mode_without_a_listing_flow_is_none(tmp_path: Path) -> None:
    """R-7.14: with no flow listing laptop_outlook there is no laptop mode."""
    cfg = _mail_config(tmp_path, None)
    store = _loaded(tmp_path, {LAPTOP_AGENT_KEY: "test-key"})

    assert _laptop_root_and_key(cfg, store) is None


def test_github_disabled_registers_no_transport_and_reads_no_secret(tmp_path: Path) -> None:
    """R-11.10: with GitHub disabled there is no transport and the store is never scoped."""
    cfg = _github_config(tmp_path, enabled=False)
    store = _RecordingStore({"other_key": "synthetic-other-value"})

    app = _github_app(cfg, store, cast(Any, None))
    transports = _github_transports(cfg, app, cast(Any, None), cast(Any, None))

    assert app is None
    assert transports == {}
    assert store.scoped_calls == []


async def test_github_enabled_missing_key_fails_startup_naming_the_key(tmp_path: Path) -> None:
    """R-11.10: with GitHub enabled and the App key missing, startup fails naming the key."""
    cfg = _github_config(tmp_path, enabled=True)
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    async with httpx.AsyncClient() as http:
        with pytest.raises(SecretMissing) as excinfo:
            _github_app(cfg, store, http)

    assert GITHUB_APP_PRIVATE_KEY in str(excinfo.value)
    assert "synthetic-other-value" not in str(excinfo.value)


async def test_github_enabled_with_key_registers_only_the_github_transport(tmp_path: Path) -> None:
    """R-11.10: with GitHub enabled and the key present, the github transport is registered."""
    cfg = _github_config(tmp_path, enabled=True)
    store = _loaded(tmp_path, {GITHUB_APP_PRIVATE_KEY: "test-key"})

    async with httpx.AsyncClient() as http:
        transports = _github_transports(cfg, _github_app(cfg, store, http), http, cast(Any, None))

    assert set(transports) == {GITHUB_ADAPTER}
