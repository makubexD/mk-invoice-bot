"""The JSON the bot and the browser helper exchange (D82).

This is the one home for the helper's wire format. It must never import Playwright, so the
bot can import it without loading a browser.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from invoicebot.config.models import MediaMaxBytes, Portal, SunatSolSite
from invoicebot.fetchers.base import FetchInvoice, FetchReason

#: The one argument a sandboxed helper accepts: run Chromium's own sandbox (D147).
CHROMIUM_SANDBOX_FLAG = "--chromium-sandbox"


class BadRequest(Exception):
    """The helper's stdin or its arguments are not one valid request."""


def parse_helper_args(argv: Sequence[str]) -> bool:
    """Return whether the helper runs Chromium with its sandbox (D147).

    Raises:
        BadRequest: the arguments are not empty and not exactly ``CHROMIUM_SANDBOX_FLAG``.
    """
    if not argv:
        return False
    if list(argv) == [CHROMIUM_SANDBOX_FLAG]:
        return True
    raise BadRequest


#: Caps on the captcha form the helper hands to the bot (D209).
MAX_CAPTCHA_FORM_BYTES = 256 * 1024
MAX_CAPTCHA_FIELDS = 200
MAX_CAPTCHA_NAME_CHARS = 128
MAX_CAPTCHA_SUBMIT_VALUE_CHARS = 256
#: The captcha PNG the sender sees is at most 64 KiB (D209).
MAX_CAPTCHA_IMAGE_BYTES = 64 * 1024
#: What the sender may type back: letters and digits, one to twelve (D209).
CAPTCHA_ANSWER_PATTERN = r"^[A-Za-z0-9]{1,12}$"


class CaptchaForm(BaseModel):
    """The portal's captcha form, frozen at the moment the helper stopped (D209)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    page_url: str
    action_url: str
    fields: tuple[tuple[str, str], ...] = Field(max_length=MAX_CAPTCHA_FIELDS)
    captcha_name: str = Field(min_length=1, max_length=MAX_CAPTCHA_NAME_CHARS)
    submit_name: str = Field(max_length=MAX_CAPTCHA_NAME_CHARS)
    submit_value: str = Field(max_length=MAX_CAPTCHA_SUBMIT_VALUE_CHARS)

    @model_validator(mode="after")
    def _check_size(self) -> Self:
        if any(len(name) > MAX_CAPTCHA_NAME_CHARS for name, _ in self.fields):
            raise ValueError("a captcha form field name is too long")
        if len(self.model_dump_json().encode()) > MAX_CAPTCHA_FORM_BYTES:
            raise ValueError("the captcha form is too large")
        return self


class CaptchaChallenge(BaseModel):
    """The captcha image the helper saved and the form it belongs to (D209)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    size: int = Field(ge=1, le=MAX_CAPTCHA_IMAGE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    form: CaptchaForm


class CaptchaResume(BaseModel):
    """The sender's answer and the form it fills, to resume a stopped fetch (D209)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    form: CaptchaForm
    answer: str = Field(pattern=CAPTCHA_ANSWER_PATTERN)


class HelperRequest(BaseModel):
    """The fetch the helper performs (D82)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    portal: Portal
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)
    resume: CaptchaResume | None = None


class SolCredentialsIn(BaseModel):
    """The SOL credentials as they arrive on stdin; their text form never shows them (R-9.16)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ruc: SecretStr
    user: SecretStr
    password: SecretStr


class SolRequest(BaseModel):
    """The SOL fetch the helper performs (D82, R-9.10)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    site: SunatSolSite
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)
    credentials: SolCredentialsIn


class HelperFile(BaseModel):
    """One file the helper downloaded, as it appears on the wire."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["pdf", "xml"]
    path: Path
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


#: The exception's class name only, never page text: a Python identifier of at most 64
#: characters (R-9.16, D169).
ERROR_CLASS_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


def error_class_of(error: BaseException) -> str | None:
    """Return the error's class name when it fits ``ERROR_CLASS_PATTERN``, else ``None`` (D169).

    The helper calls this inside its top-level ``except``, so a name the model would
    reject must not raise there: the result line is still printed, without the class.
    """
    name = type(error).__name__
    return name if re.fullmatch(ERROR_CLASS_PATTERN, name, flags=re.ASCII) else None


class HelperOutput(BaseModel):
    """The helper's one stdout line: the reason, its files and a failure's class (S10.1, D169).

    ``error_class`` names the exception a top-level boundary caught and is null otherwise;
    it holds an identifier only, never page text (R-9.16, D169).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: FetchReason
    files: tuple[HelperFile, ...] = ()
    error_class: str | None = Field(default=None, pattern=ERROR_CLASS_PATTERN)
    captcha: CaptchaChallenge | None = None

    @model_validator(mode="after")
    def _captcha_iff_asked(self) -> Self:
        if (self.captcha is not None) != (self.reason is FetchReason.CAPTCHA_ASKED):
            raise ValueError("captcha is present exactly when the reason is captcha_asked")
        return self
