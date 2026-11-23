"""Unit tests for the shared intake route (R-1.3)."""

from __future__ import annotations

from invoicebot.api.intake_route import parse_json


def test_deeply_nested_body_is_refused_instead_of_raising() -> None:
    """R-1.3: a body nested past the decoder's recursion limit gets 400, not a crash."""
    assert parse_json(b"[" * 100_000) is None
