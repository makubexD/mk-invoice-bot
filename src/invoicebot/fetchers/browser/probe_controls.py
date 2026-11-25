"""Read the form of the page the probe has open (D193).

Before the probe types anything it records what the current page offers: the visible
inputs and selects of its first real form, their labels and the control that submits
them, plus whether the page is a login or asks for a captcha. Nothing here navigates
or clicks; reaching the form is another module's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from string import Template
from typing import Annotated
from urllib.parse import urlsplit

from playwright.async_api import Page
from pydantic import BaseModel, ConfigDict, Field

from invoicebot.fetchers.browser.field_match import fold, value_name, words
from invoicebot.fetchers.browser.probe_protocol import (
    MAX_FIELD_OPTIONS,
    MAX_REPORT_FIELDS,
    SELECTOR_PATTERN,
    CaptchaKind,
    FieldKind,
    ProbedField,
    ValueName,
    safe_text,
)

#: How many controls, buttons and options one page may hand back (D193).
MAX_CONTROLS = 100
MAX_BUTTONS = 50
MAX_OPTIONS = 100
#: How long one raw string from the page may be (D193).
MAX_RAW_TEXT = 200

#: Hosts whose ``/recaptcha`` iframe is a reCAPTCHA widget (D193).
_RECAPTCHA_HOSTS = frozenset({"google.com", "www.google.com", "www.recaptcha.net"})
_RECAPTCHA_PATH = "/recaptcha"
_HCAPTCHA_HOST = "hcaptcha.com"
#: Whole words that mark the control that submits a search form (D193).
_SUBMIT_WORDS = frozenset({"buscar", "consultar", "ver"})

#: Control types the probe never reports as fields, only notices when required (D193).
_UNREAD_TYPES = frozenset({"textarea", "radio", "checkbox"})
#: Folded starts of a select's placeholder option, which is not a real choice (D193).
_PLACEHOLDER_STARTS = ("selec", "elij", "escoj", "--")

_RawText = Annotated[str, Field(max_length=MAX_RAW_TEXT)]


class _RawControl(BaseModel):
    """One visible control as the page reports it, before anything is trusted (D193)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group: int
    id: _RawText
    name: _RawText
    type: _RawText
    label: _RawText
    options: tuple[_RawText, ...] = Field(default=(), max_length=MAX_OPTIONS)
    option_values: tuple[_RawText, ...] = Field(default=(), max_length=MAX_OPTIONS)
    required: bool = False


class _RawButton(BaseModel):
    """One visible button as the page reports it (D193)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    group: int
    id: _RawText
    name: _RawText
    text: _RawText
    submit: bool


class _RawPage(BaseModel):
    """Everything one script run reads from the main frame (D193)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    controls: tuple[_RawControl, ...] = Field(max_length=MAX_CONTROLS)
    buttons: tuple[_RawButton, ...] = Field(max_length=MAX_BUTTONS)
    has_password: bool
    iframes: tuple[_RawText, ...] = Field(max_length=MAX_CONTROLS)
    recaptcha_class: bool
    hcaptcha_class: bool
    image_captcha: bool


#: One evaluate over the main frame; every value it returns is capped here (D193).
_READ_PAGE_JS = Template(
    """
() => {
  const maxControls = $max_controls;
  const maxButtons = $max_buttons;
  const maxOptions = $max_options;
  const maxText = $max_text;
  const cut = (value) => String(value == null ? "" : value).slice(0, maxText);
  const isVisible = (element) =>
    element.getClientRects().length > 0 &&
    window.getComputedStyle(element).visibility === "visible";
  const groupOf = (element) =>
    element.form ? Array.prototype.indexOf.call(document.forms, element.form) : -1;
  const labelOf = (element) => {
    if (element.id) {
      for (const label of document.querySelectorAll("label[for]")) {
        if (label.htmlFor === element.id) {
          return label.textContent;
        }
      }
    }
    const enclosing = element.closest("label");
    if (enclosing) {
      return enclosing.textContent;
    }
    if (element.getAttribute("aria-label")) {
      return element.getAttribute("aria-label");
    }
    if (element.getAttribute("placeholder")) {
      return element.getAttribute("placeholder");
    }
    const cell = element.closest("td,th");
    if (cell) {
      const previous = cell.previousElementSibling;
      if (previous && (previous.tagName === "TD" || previous.tagName === "TH")) {
        return previous.textContent;
      }
    }
    return "";
  };
  const textTypes = new Set(["text", "search", "tel", "number", "date", "email"]);
  const unreadTypes = new Set(["radio", "checkbox"]);
  const controls = [];
  for (const element of document.querySelectorAll("input, select, textarea")) {
    if (controls.length >= maxControls) {
      break;
    }
    if (element.disabled || !isVisible(element)) {
      continue;
    }
    let kind = (element.getAttribute("type") || "").toLowerCase();
    if (element.tagName === "SELECT") {
      kind = "select";
    } else if (element.tagName === "TEXTAREA") {
      kind = "textarea";
    } else if (unreadTypes.has(kind)) {
      // kept only so a required one can be noticed; never reported as a field
    } else if (kind !== "" && !textTypes.has(kind)) {
      continue;
    } else if (kind === "") {
      kind = "text";
    }
    const options =
      kind === "select"
        ? Array.from(element.options).slice(0, maxOptions).map((option) => cut(option.textContent))
        : [];
    const optionValues =
      kind === "select"
        ? Array.from(element.options).slice(0, maxOptions).map((option) => cut(option.value))
        : [];
    controls.push({
      group: groupOf(element),
      required: element.required === true || element.getAttribute("aria-required") === "true",
      option_values: optionValues,
      id: cut(element.id),
      name: cut(element.getAttribute("name")),
      type: cut(kind),
      label: cut(labelOf(element)),
      options: options,
    });
  }
  const buttons = [];
  const buttonTypes = new Set(["submit", "image", "button"]);
  for (const element of document.querySelectorAll("button, input")) {
    if (buttons.length >= maxButtons) {
      break;
    }
    const isButton = element.tagName === "BUTTON";
    const kind = (element.getAttribute("type") || "").toLowerCase();
    if (!isButton && !buttonTypes.has(kind)) {
      continue;
    }
    if (element.disabled || !isVisible(element)) {
      continue;
    }
    buttons.push({
      group: groupOf(element),
      id: cut(element.id),
      name: cut(element.getAttribute("name")),
      text: cut(isButton ? element.textContent : element.value),
      submit: isButton
        ? kind === "" || kind === "submit"
        : kind === "submit" || kind === "image",
    });
  }
  const hasPassword = Array.from(document.querySelectorAll("input")).some(
    (element) =>
      (element.getAttribute("type") || "").toLowerCase() === "password" &&
      isVisible(element)
  );
  const iframes = Array.from(document.querySelectorAll("iframe"))
    .slice(0, maxControls)
    .map((element) => cut(element.src));
  const imageCaptcha = Array.from(document.querySelectorAll("img, input")).some((element) => {
    const mark = [
      element.id || "",
      element.getAttribute("name") || "",
      element.getAttribute("src") || "",
    ]
      .join(" ")
      .toLowerCase();
    return mark.includes("captcha");
  });
  return {
    controls: controls,
    buttons: buttons,
    has_password: hasPassword,
    iframes: iframes,
    recaptcha_class: document.getElementsByClassName("g-recaptcha").length > 0,
    hcaptcha_class: document.getElementsByClassName("h-captcha").length > 0,
    image_captcha: imageCaptcha,
  };
}
"""
).substitute(
    max_controls=MAX_CONTROLS,
    max_buttons=MAX_BUTTONS,
    max_options=MAX_OPTIONS,
    max_text=MAX_RAW_TEXT,
)


@dataclass(frozen=True)
class PageForm:
    """The form the current page shows, read before anything is typed (D193).

    ``fields`` is empty when the page has no form. ``required_unmatched`` is true when
    a visible enabled required control of the form is one the probe cannot fill from
    the invoice: it has no value name, no unique selector, lies past the field cap, or
    is a textarea, radio or checkbox (an unmatched select with one real option is
    fine). ``captcha`` is ``none`` when the
    page asks for no captcha; ``has_password`` is true when a visible password input
    is anywhere on the page, even outside the form.
    """

    captcha: CaptchaKind
    has_password: bool
    fields: tuple[ProbedField, ...]
    submit: str | None
    required_unmatched: bool = False


async def read_page(page: Page) -> PageForm:
    """Read the current page's form, captcha and password field (D193).

    The form is the first group (a ``<form>`` index, or ``-1`` for a control outside
    any form) with at least two controls that map to an invoice value. Its fields are
    every visible control of that group in document order, matched or not, up to
    ``MAX_REPORT_FIELDS``; a control with no unique selector is left out.

    Raises:
        PlaywrightError: the page or one of its frames could not be evaluated.
    """
    raw = _RawPage.model_validate(await page.evaluate(_READ_PAGE_JS))
    grouped = _grouped_controls(raw.controls)
    form_group = _form_group(grouped)
    if form_group is None:
        return PageForm(captcha=_captcha_kind(raw), has_password=raw.has_password, fields=(), submit=None)
    fields, required_unmatched = await _fields(page, grouped[form_group])
    submit = await _submit(page, raw.buttons, form_group)
    return PageForm(
        captcha=_captcha_kind(raw),
        has_password=raw.has_password,
        fields=fields,
        submit=submit,
        required_unmatched=required_unmatched,
    )


def _grouped_controls(
    controls: Sequence[_RawControl],
) -> dict[int, list[tuple[_RawControl, ValueName | None]]]:
    """Group the raw controls by form index, carrying each one's invoice value (D193)."""
    grouped: dict[int, list[tuple[_RawControl, ValueName | None]]] = {}
    for control in controls:
        grouped.setdefault(control.group, []).append((control, _matched_value(control)))
    return grouped


def _matched_value(control: _RawControl) -> ValueName | None:
    """Return the invoice value *control* carries, matching its raw label first (D193)."""
    if control.type in _UNREAD_TYPES:
        return None
    return value_name(control.label, control.name or control.id)


def _form_group(
    grouped: Mapping[int, Sequence[tuple[_RawControl, ValueName | None]]],
) -> int | None:
    """Return the first group with two controls that map to an invoice value (D193)."""
    for group, members in grouped.items():
        if sum(1 for _, matched in members if matched is not None) >= 2:
            return group
    return None


async def _fields(
    page: Page, members: Sequence[tuple[_RawControl, ValueName | None]]
) -> tuple[tuple[ProbedField, ...], bool]:
    """Turn a form group's controls into report fields and flag required ones left out (D193).

    A control with no unique selector, or of a type not read, is skipped. The flag is
    true when a required control is skipped, has no value name (see
    :func:`_needs_a_value`) or lies past ``MAX_REPORT_FIELDS``.
    """
    fields: list[ProbedField] = []
    required_unmatched = False
    for control, matched in members[:MAX_REPORT_FIELDS]:
        field = await _field(page, control, matched)
        if field is not None:
            fields.append(field)
        if control.required and _needs_a_value(control, matched, field):
            required_unmatched = True
    if any(control.required for control, _ in members[MAX_REPORT_FIELDS:]):
        required_unmatched = True
    return tuple(fields), required_unmatched


async def _field(page: Page, control: _RawControl, matched: ValueName | None) -> ProbedField | None:
    """Return the report field of a control, or None when it has none to report (D193)."""
    if control.type in _UNREAD_TYPES:
        return None
    selector = await _locate(page, control.id, control.name)
    if selector is None:
        return None
    return ProbedField(
        selector=selector,
        kind=_kind(control.type),
        label=safe_text(control.label),
        value_name=matched,
        options=_options(control),
    )


def _needs_a_value(control: _RawControl, matched: ValueName | None, field: ProbedField | None) -> bool:
    """Return whether a required control is one the probe cannot fill (D193)."""
    if field is None:
        return True
    if matched is not None:
        return False
    return not (control.type == "select" and _real_options(control) == 1)


def _real_options(control: _RawControl) -> int:
    """Count a select's real options: a non-blank value and text that is not a placeholder (D193)."""
    count = 0
    for text, value in zip(control.options, control.option_values, strict=False):
        folded = fold(text.strip())
        if value.strip() and folded and not folded.startswith(_PLACEHOLDER_STARTS):
            count += 1
    return count


def _kind(raw_type: str) -> FieldKind:
    """Return the report kind of a control from its raw type (D193)."""
    if raw_type == "select":
        return "select"
    if raw_type == "date":
        return "date"
    return "text"


def _options(control: _RawControl) -> tuple[str, ...]:
    """Return the control's option labels reduced to safe text, empty ones dropped (D193)."""
    texts = (safe_text(option) for option in control.options)
    return tuple(text for text in texts if text)[:MAX_FIELD_OPTIONS]


async def _submit(page: Page, buttons: Sequence[_RawButton], group: int) -> str | None:
    """Return the selector of the group's one submit button, or None (D193).

    A single submit control wins. Otherwise the only visible button whose text has
    the whole word ``buscar``, ``consultar`` or ``ver`` wins; two or none give None.
    """
    members = [button for button in buttons if button.group == group]
    submitters = [button for button in members if button.submit]
    if len(submitters) == 1:
        winner = submitters[0]
    else:
        hits = [button for button in members if _has_submit_word(button.text)]
        if len(hits) != 1:
            return None
        winner = hits[0]
    return await _locate(page, winner.id, winner.name)


def _has_submit_word(text: str) -> bool:
    """Return whether *text* has a whole submit word, accents and case dropped (D193)."""
    return bool(words(text) & _SUBMIT_WORDS)


async def _locate(page: Page, control_id: str, name: str) -> str | None:
    """Return ``#id`` or ``[name=...]`` when exactly one element matches, else None (D193)."""
    for candidate in _selector_candidates(control_id, name):
        if await page.locator(candidate).count() == 1:
            return candidate
    return None


def _selector_candidates(control_id: str, name: str) -> tuple[str, ...]:
    """Return the selectors that fit the wire format, id first (D193)."""
    candidates: list[str] = []
    by_id = f"#{control_id}"
    if re.fullmatch(SELECTOR_PATTERN, by_id):
        candidates.append(by_id)
    by_name = f"[name='{name}']"
    if re.fullmatch(SELECTOR_PATTERN, by_name):
        candidates.append(by_name)
    return tuple(candidates)


def _captcha_kind(raw: _RawPage) -> CaptchaKind:
    """Return the page's captcha, in the order reCAPTCHA, hCaptcha, image (D193).

    Visibility does not matter: a hidden widget still means the page asks for a captcha.
    """
    if raw.recaptcha_class or any(_is_recaptcha(src) for src in raw.iframes):
        return "recaptcha"
    if raw.hcaptcha_class or any(_is_hcaptcha(src) for src in raw.iframes):
        return "hcaptcha"
    if raw.image_captcha:
        return "image"
    return "none"


def _is_recaptcha(src: str) -> bool:
    """Return whether an iframe source is a reCAPTCHA widget (D193)."""
    host, path = _url_host_path(src)
    return host in _RECAPTCHA_HOSTS and path.startswith(_RECAPTCHA_PATH)


def _is_hcaptcha(src: str) -> bool:
    """Return whether an iframe source is an hCaptcha widget (D193)."""
    host, _ = _url_host_path(src)
    return host == _HCAPTCHA_HOST or host.endswith(f".{_HCAPTCHA_HOST}")


def _url_host_path(src: str) -> tuple[str, str]:
    """Return an iframe source's lowercase host and path, or two empty strings (D193)."""
    try:
        parts = urlsplit(src)
        return (parts.hostname or "").lower(), parts.path
    except ValueError:
        return "", ""
