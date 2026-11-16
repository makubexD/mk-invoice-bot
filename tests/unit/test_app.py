"""Unit tests for the app wiring (R-7.12, R-7.14)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pyrage
import pytest
import yaml

from invoicebot.app import LAPTOP_AGENT_KEY, _laptop_mode, _payment_flow
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.commands import PaymentFlow
from invoicebot.secrets import SecretMissing, SecretStore
from invoicebot.sinks.mailer import LaptopOutlook

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "example.yaml"
#: The example flow that carries reimbursement settings.
REIMBURSING_FLOW = "work_microsoft"


class _FakeDrive:
    """A Drive stand-in; the laptop mode only keeps it until a delivery needs it (R-7.14)."""

    async def upload(self, path: str, data: bytes) -> None:
        """Accept an upload; these tests never deliver."""


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


def test_payment_flow_tracking_flow_gives_its_id_and_cap(tmp_path: Path) -> None:
    """R-7.12: the flow that tracks payments supplies its id and cap to commands."""
    cfg = _config(tmp_path, track_payments=True)

    assert _payment_flow(cfg) == PaymentFlow(flow_id=REIMBURSING_FLOW, cap=Decimal("50.00"))


def test_payment_flow_with_no_tracking_flow_is_none(tmp_path: Path) -> None:
    """R-7.12: with no flow tracking payments, payment commands stay off."""
    cfg = _config(tmp_path, track_payments=False)

    assert _payment_flow(cfg) is None


def test_laptop_mode_without_the_agent_key_raises_secret_missing(tmp_path: Path) -> None:
    """R-7.14: a flow listing laptop_outlook without the agent key stops startup."""
    cfg = _mail_config(tmp_path, "laptop_outlook")
    store = _loaded(tmp_path, {"other_key": "synthetic-other-value"})

    with pytest.raises(SecretMissing):
        _laptop_mode(cfg, store, _FakeDrive())


def test_laptop_mode_with_the_agent_key_returns_the_mode(tmp_path: Path) -> None:
    """R-7.14: a flow listing laptop_outlook builds the mode from the key and the drive."""
    cfg = _mail_config(tmp_path, "laptop_outlook")
    store = _loaded(tmp_path, {LAPTOP_AGENT_KEY: "test-key"})

    assert isinstance(_laptop_mode(cfg, store, _FakeDrive()), LaptopOutlook)


def test_laptop_mode_without_a_listing_flow_is_none(tmp_path: Path) -> None:
    """R-7.14: with no flow listing laptop_outlook there is no laptop mode."""
    cfg = _mail_config(tmp_path, None)
    store = _loaded(tmp_path, {LAPTOP_AGENT_KEY: "test-key"})

    assert _laptop_mode(cfg, store, _FakeDrive()) is None
