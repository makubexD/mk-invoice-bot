"""The JSON the bot and the portal probe helper exchange (D193, R-11.12).

This is the one home of the probe's wire format. It must never import Playwright, so
the bot can import it without loading a browser.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from invoicebot.config.models import MediaMaxBytes
from invoicebot.fetchers.base import FetchInvoice
from invoicebot.fetchers.browser.protocol import ERROR_CLASS_PATTERN, HelperFile

#: A form control found by id or by name, ASCII only (D193, R-11.12).
SELECTOR_PATTERN = r"^(#[A-Za-z][A-Za-z0-9_-]{0,79}|\[name='[A-Za-z0-9_$:.-]{1,80}'\])$"
#: A label or option reduced to the allow-list of characters (D193, R-11.12).
SAFE_TEXT_PATTERN = r"^[A-Za-z0-9ÁÉÍÓÚÜÑáéíóúüñ .,()/°-]{0,60}$"
#: The form's path on the approved host, never its query (D193, R-11.12).
FORM_PATH_PATTERN = r"^/[A-Za-z0-9._~/-]{0,200}$"

#: A report never carries more fields than this (D193).
MAX_REPORT_FIELDS = 20
#: A select never carries more option labels than this (D193).
MAX_FIELD_OPTIONS = 20

#: A control the probe can point the helper at, by id or by name (D193, R-11.12).
Selector = Annotated[str, Field(pattern=SELECTOR_PATTERN)]
#: A label or option reduced to the allow-list of characters (D193, R-11.12).
SafeText = Annotated[str, Field(pattern=SAFE_TEXT_PATTERN)]

FieldKind = Literal["text", "select", "date"]
ValueName = Literal["ruc_emisor", "tipo", "serie", "numero", "fecha", "total"]
CaptchaKind = Literal["none", "image", "recaptcha", "hcaptcha"]


class ProbeOutcome(StrEnum):
    """How one probe ended (D193, R-11.12)."""

    OK = "ok"
    CAPTCHA = "captcha"
    UNMATCHED_FIELD = "unmatched_field"
    LOGIN_ONLY = "login_only"
    NO_FORM = "no_form"
    OTHER_HOST = "other_host"
    NOT_FOUND = "not_found"
    NO_DOWNLOAD = "no_download"
    BUSY = "busy"
    GUARD_BLOCKED = "guard_blocked"
    TECHNICAL = "technical"


class ProbeRequest(BaseModel):
    """The probe the helper performs (D193, R-11.12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    invoice: FetchInvoice
    out_dir: Path
    max_bytes: MediaMaxBytes
    page_timeout_ms: int = Field(gt=0)


class ProbedField(BaseModel):
    """One form field the probe read, with its label and never its value (D193, R-11.12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    selector: Selector
    kind: FieldKind
    label: SafeText
    value_name: ValueName | None = None
    options: tuple[SafeText, ...] = Field(default=(), max_length=MAX_FIELD_OPTIONS)


class ProbeReport(BaseModel):
    """The helper's one stdout line for a probe (D193, R-11.12)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: ProbeOutcome
    form_path: str | None = Field(default=None, pattern=FORM_PATH_PATTERN)
    captcha: CaptchaKind = "none"
    fields: tuple[ProbedField, ...] = Field(default=(), max_length=MAX_REPORT_FIELDS)
    submit: Selector | None = None
    pdf: Selector | None = None
    xml: Selector | None = None
    files: tuple[HelperFile, ...] = ()
    error_class: str | None = Field(default=None, pattern=ERROR_CLASS_PATTERN)
