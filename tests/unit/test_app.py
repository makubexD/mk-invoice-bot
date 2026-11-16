"""Unit tests for the app wiring (R-7.12)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from invoicebot.app import _payment_flow
from invoicebot.config.loader import load_config
from invoicebot.config.models import Config
from invoicebot.pipeline.commands import PaymentFlow

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


def test_payment_flow_tracking_flow_gives_its_id_and_cap(tmp_path: Path) -> None:
    """R-7.12: the flow that tracks payments supplies its id and cap to commands."""
    cfg = _config(tmp_path, track_payments=True)

    assert _payment_flow(cfg) == PaymentFlow(flow_id=REIMBURSING_FLOW, cap=Decimal("50.00"))


def test_payment_flow_with_no_tracking_flow_is_none(tmp_path: Path) -> None:
    """R-7.12: with no flow tracking payments, payment commands stay off."""
    cfg = _config(tmp_path, track_payments=False)

    assert _payment_flow(cfg) is None
