"""Tests for the helper's wire format (D169)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from invoicebot.fetchers.browser.protocol import HelperOutput, error_class_of


def test_helper_output_keeps_the_error_class() -> None:
    """D169: the helper's failure class survives the wire format."""
    output = HelperOutput.model_validate_json('{"reason": "technical", "error_class": "TargetClosedError"}')

    assert output.error_class == "TargetClosedError"


def test_helper_output_error_class_is_optional() -> None:
    """D169: an output without an error class has none."""
    output = HelperOutput.model_validate_json('{"reason": "technical"}')

    assert output.error_class is None


@pytest.mark.parametrize("error_class", ["a b", "1Error", "Error: page text", "", "E" * 65])
def test_helper_output_rejects_a_non_identifier_error_class(error_class: str) -> None:
    """D169: an error class that is not a Python identifier is rejected."""
    payload = json.dumps({"reason": "technical", "error_class": error_class})

    with pytest.raises(ValidationError):
        HelperOutput.model_validate_json(payload)


def test_error_class_of_names_an_ordinary_exception() -> None:
    """D169: an ordinary exception's class name is kept."""
    assert error_class_of(ValueError("page text")) == "ValueError"


@pytest.mark.parametrize("name", ["E" * 65, "Ërror"])
def test_error_class_of_drops_a_name_the_model_would_reject(name: str) -> None:
    """D169: a class name outside the pattern gives None, so the helper's except block cannot raise."""
    error_type = type(name, (Exception,), {})

    assert error_class_of(error_type()) is None
