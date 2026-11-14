"""Tests for the safe template expressions (R-7.1, R-7.2, R-7.6)."""

from __future__ import annotations

from datetime import date

import pytest

from invoicebot.sinks.expr import ExprError, field_names, render


def test_render_folder_template() -> None:
    """R-7.1: the folder template renders the month, seq and comercio."""
    template = "{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}"
    values = {"fecha": date(2026, 11, 5), "seq": 1, "comercio": "LaLena"}
    assert render(template, values) == "2026-11-Support_1_LaLena"


def test_render_attribute_from_mapping() -> None:
    """R-7.2: profile.legal_name reads the nested mapping key."""
    assert render("{profile.legal_name}.pdf", {"profile": {"legal_name": "LaLena SAC"}}) == "LaLena SAC.pdf"


def test_render_date_in_subject() -> None:
    """R-7.6: the subject template formats the date with strftime specs."""
    assert render("Support release {fecha:%d/%m/%Y}", {"fecha": date(2026, 11, 5)}) == (
        "Support release 05/11/2026"
    )


@pytest.mark.parametrize(
    ("template", "values"),
    [
        ("{x[0]}", {"x": [1]}),
        ("{x!r}", {"x": 1}),
        ("{x.__class__}", {"x": 1}),
        ("{a.b.c}", {"a": {"b": {"c": 1}}}),
        ("{x:{y}}", {"x": 1, "y": "d"}),
        ("{missing}", {}),
    ],
)
def test_render_rejects_unsafe_fields(template: str, values: dict[str, object]) -> None:
    """R-7.1, R-7.2, R-7.6: unsafe or unknown field forms raise ExprError."""
    with pytest.raises(ExprError):
        render(template, values)


def test_field_names_of_folder_template() -> None:
    """R-7.1: field_names returns the top-level names the folder template uses."""
    template = "{fecha:%Y}-{fecha:%m}-Support_{seq}_{comercio}"
    assert field_names(template) == frozenset({"fecha", "seq", "comercio"})
