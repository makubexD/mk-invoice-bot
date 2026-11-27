"""Unit tests for the config-derived intake settings (D165, D171)."""

from pathlib import Path
from typing import Literal

from invoicebot.config.loader import load_config
from invoicebot.config.models import Config, MailerSink
from invoicebot.pipeline.intake_config import intake_settings, review_flow_ids

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "example.yaml"


def test_intake_settings_carry_the_reprocess_interval() -> None:
    """D171: the /reprocesar interval reaches the command context's roles from the config."""
    cfg = load_config(EXAMPLE)

    settings = intake_settings(cfg)

    assert settings.roles.reprocess_min_interval_s == cfg.limits.reprocess_min_interval_s == 600


def test_review_flow_ids_name_the_flows_whose_mailer_waits_for_review() -> None:
    """D194, D203: a flow is a review flow only when its mailer is ``send: review`` or ``if_doubtful``."""
    cfg = load_config(EXAMPLE)
    mailer_flows = {
        name for name, flow in cfg.flows.items() if any(isinstance(sink, MailerSink) for sink in flow.sinks)
    }

    def with_send(send: Literal["review", "auto", "if_doubtful"]) -> Config:
        flows = {
            name: flow.model_copy(
                update={
                    "sinks": [
                        sink.model_copy(update={"send": send}) if isinstance(sink, MailerSink) else sink
                        for sink in flow.sinks
                    ]
                }
            )
            for name, flow in cfg.flows.items()
        }
        return cfg.model_copy(update={"flows": flows})

    assert mailer_flows
    assert review_flow_ids(with_send("review")) == mailer_flows
    assert review_flow_ids(with_send("if_doubtful")) == mailer_flows
    assert review_flow_ids(with_send("auto")) == frozenset()
