"""Tests for the invoice state sets in ``invoicebot.states`` (spec 03 §4)."""

from invoicebot import states
from invoicebot.rules.messages_es import STATE_DESC


def test_all_states_names_every_state_constant() -> None:
    """D188, spec 03 §4: ALL_STATES holds every upper-case state name the module defines."""
    names = {value for name, value in vars(states).items() if name.isupper() and isinstance(value, str)}
    assert names == states.ALL_STATES


def test_every_state_has_a_description() -> None:
    """D188, S4.9: every state the [Ver] button may answer for has a Spanish description."""
    assert set(STATE_DESC) == states.ALL_STATES
