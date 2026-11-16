"""WhatsApp transport template slots (R-1.10)."""

import pytest

from invoicebot.channels.base import OutboundMessage
from invoicebot.channels.whatsapp.transport import template_slot

NEW_ADMIN_ALERT_RULES = (
    "GAP_admin",
    "GAP_DIGEST_admin",
    "BR-O02",
    "BR-O03",
    "BR-O04",
    "BR-D05",
)


def _message(rule_id: str) -> OutboundMessage:
    return OutboundMessage(
        channel="whatsapp",
        recipient="sender",
        profile_id="profile",
        rule_id=rule_id,
        text="texto",
    )


@pytest.mark.parametrize("rule_id", NEW_ADMIN_ALERT_RULES)
def test_new_admin_alert_rule_resolves_to_admin_alert(rule_id: str) -> None:
    """R-1.10: the gap and operations alerts use the approved admin_alert template."""
    assert template_slot(_message(rule_id)) == "admin_alert"
