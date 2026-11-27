"""Tests for the helper's wire format (D169)."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.protocol import (
    MAX_CAPTCHA_FORM_BYTES,
    MAX_CAPTCHA_IMAGE_BYTES,
    CaptchaForm,
    CaptchaResume,
    HelperOutput,
    HelperRequest,
    error_class_of,
)

SHA256 = "a" * 64


def _form(**overrides: object) -> dict[str, object]:
    form: dict[str, object] = {
        "page_url": "https://portal.example/consulta",
        "action_url": "https://portal.example/consulta",
        "fields": [["serie", "F001"], ["token", "x"]],
        "submit_name": "btn",
        "submit_value": "Buscar",
    }
    return form | overrides


def _challenge(**overrides: object) -> dict[str, object]:
    challenge: dict[str, object] = {"path": "captcha.png", "size": 100, "sha256": SHA256, "form": _form()}
    return challenge | overrides


def _request(resume: CaptchaResume | None) -> HelperRequest:
    return HelperRequest(
        portal=Portal(key="synthetic", adapter="generic_form"),
        invoice=FetchInvoice(
            ruc_emisor="20123456789",
            tipo="01",
            serie="F001",
            numero="123",
            fecha=date(2026, 11, 5),
            total=Decimal("118.00"),
        ),
        out_dir=Path("out"),
        max_bytes=MediaMaxBytes(image=10, pdf=20, xml=5, zip=50),
        page_timeout_ms=1000,
        resume=resume,
    )


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


def test_captcha_asked_output_needs_a_captcha() -> None:
    """D209: a captcha_asked output without its captcha is refused."""
    with pytest.raises(ValidationError):
        HelperOutput.model_validate_json('{"reason": "captcha_asked"}')


def test_captcha_asked_output_with_a_captcha_passes() -> None:
    """D209: a captcha_asked output with its captcha is accepted."""
    payload = json.dumps({"reason": "captcha_asked", "captcha": _challenge()})

    assert HelperOutput.model_validate_json(payload).captcha is not None


def test_ok_output_refuses_a_captcha() -> None:
    """D209: an ok output that carries a captcha is refused."""
    payload = json.dumps({"reason": "ok", "captcha": _challenge()})

    with pytest.raises(ValidationError):
        HelperOutput.model_validate_json(payload)


def test_captcha_form_over_the_byte_cap_is_refused() -> None:
    """D209: a form whose JSON is over 256 KiB is refused."""
    fields = [[f"n{i}", "v" * 2000] for i in range(200)]
    assert len(json.dumps(fields)) > MAX_CAPTCHA_FORM_BYTES

    with pytest.raises(ValidationError):
        CaptchaForm.model_validate(_form(fields=fields))


@pytest.mark.parametrize("size", [0, MAX_CAPTCHA_IMAGE_BYTES + 1])
def test_captcha_image_outside_its_bounds_is_refused(size: int) -> None:
    """D209: an image of 0 bytes or 64 KiB + 1 is refused."""
    payload = json.dumps({"reason": "captcha_asked", "captcha": _challenge(size=size)})

    with pytest.raises(ValidationError):
        HelperOutput.model_validate_json(payload)


def test_captcha_image_at_the_cap_passes() -> None:
    """D209: an image of exactly 64 KiB is accepted."""
    payload = json.dumps({"reason": "captcha_asked", "captcha": _challenge(size=MAX_CAPTCHA_IMAGE_BYTES)})

    assert HelperOutput.model_validate_json(payload).captcha is not None


@pytest.mark.parametrize("answer", ["ab 12", "", "a" * 13])
def test_captcha_answer_outside_the_pattern_is_refused(answer: str) -> None:
    """D209: an answer with a space, empty or of 13 letters is refused."""
    with pytest.raises(ValidationError):
        _request(CaptchaResume.model_validate({"form": _form(), "answer": answer}))


def test_captcha_answer_inside_the_pattern_passes() -> None:
    """D209: the answer ab12 is accepted."""
    assert _request(CaptchaResume.model_validate({"form": _form(), "answer": "ab12"})).resume is not None


def test_request_with_resume_round_trips() -> None:
    """D209: a request with a resume keeps every field through JSON."""
    request = _request(CaptchaResume.model_validate({"form": _form(), "answer": "ab12"}))

    assert HelperRequest.model_validate_json(request.model_dump_json()) == request
