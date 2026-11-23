"""Unit tests for the config-derived intake settings (D165, D171)."""

from pathlib import Path

from invoicebot.config.loader import load_config
from invoicebot.pipeline.intake_config import intake_settings

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


def test_intake_settings_carry_the_reprocess_interval() -> None:
    """D171: the /reprocesar interval reaches the command context's roles from the config."""
    cfg = load_config(EXAMPLE)

    settings = intake_settings(cfg)

    assert settings.roles.reprocess_min_interval_s == cfg.limits.reprocess_min_interval_s == 600
