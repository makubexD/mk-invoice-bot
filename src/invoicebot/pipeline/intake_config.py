"""Build the intake settings from the config, without importing the web app or an adapter.

The webhook and the console CLI feed the same intake, so the per-message knobs and the
roles a message may use are derived here once (D165). Keeping the derivation out of
``app`` lets a CLI command read it without building the FastAPI app.
"""

from invoicebot.config.models import Config, Flow, MailerSink
from invoicebot.net.guard import SystemResolver
from invoicebot.pipeline.commands import PaymentFlow
from invoicebot.pipeline.intake import IntakeRoles, IntakeSettings
from invoicebot.pipeline.teach import TeachSetup

#: The adapter and mail mode that hands the mail to the laptop agent (R-7.14, D108).
LAPTOP_ADAPTER = "laptop_outlook"


def teach_setup(cfg: Config) -> TeachSetup | None:
    """Return the teaching setup, or None when chat teaching is off (R-11.4, BR-F02)."""
    if not cfg.gaps.teach_via_chat:
        return None
    return TeachSetup(
        profiles=cfg.profiles,
        resolver=SystemResolver(),
        github_repo=cfg.gaps.github.repo if cfg.gaps.github.enabled else None,
    )


def payment_flow(cfg: Config) -> PaymentFlow | None:
    """Return the first flow that tracks payments, or None when no flow opts in (R-7.12)."""
    for name, flow in cfg.flows.items():
        reimbursement = flow.reimbursement
        if reimbursement is not None and reimbursement.track_payments:
            return PaymentFlow(flow_id=name, cap=reimbursement.cap)
    return None


def admin_profiles(cfg: Config) -> frozenset[str]:
    """Return the profile ids whose ``admin`` flag is set (R-1.2)."""
    return frozenset(name for name, profile in cfg.profiles.items() if profile.admin)


def lists_laptop(flow: Flow) -> bool:
    """Return whether the flow's mailer sink lists the laptop mode (R-7.14)."""
    return any(isinstance(sink, MailerSink) and LAPTOP_ADAPTER in sink.modes for sink in flow.sinks)


def laptop_flow_ids(cfg: Config) -> frozenset[str]:
    """Return the names of the flows whose mailer sink lists ``laptop_outlook`` (D109)."""
    return frozenset(name for name, flow in cfg.flows.items() if lists_laptop(flow))


#: The mailer ``send`` settings that can hold a mail for review (D194, D203).
_REVIEW_SENDS = frozenset({"review", "if_doubtful"})


def review_flow_ids(cfg: Config) -> frozenset[str]:
    """Return the names of the flows whose mailer can post BR-D19 for review (D194, D203).

    Those are the mailers on ``send: review`` or ``send: if_doubtful``.
    """
    return frozenset(
        name
        for name, flow in cfg.flows.items()
        if any(isinstance(sink, MailerSink) and sink.send in _REVIEW_SENDS for sink in flow.sinks)
    )


def intake_settings(cfg: Config) -> IntakeSettings:
    """Return the intake arguments shared by the webhook and the console CLI (D165)."""
    return IntakeSettings(
        max_queued_per_sender=cfg.limits.max_queued_per_sender,
        media_max_bytes=cfg.limits.media.max_bytes,
        timezone=cfg.owner.timezone,
        roles=IntakeRoles(
            admins=admin_profiles(cfg),
            payment_flow=payment_flow(cfg),
            laptop_flows=laptop_flow_ids(cfg),
            review_flows=review_flow_ids(cfg),
            teach=teach_setup(cfg),
            reprocess_min_interval_s=cfg.limits.reprocess_min_interval_s,
        ),
    )
