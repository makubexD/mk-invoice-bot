"""Reach the portal's form from the link the sender taught (D193).

The probe starts on the taught link, reads each page with :func:`read_page`, and
follows one promising link or button at a time until the invoice form shows. It
never types anything and never leaves the approved host. This module only reaches
the form; filling it is another module's job.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from string import Template
from typing import Annotated
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, ConfigDict, Field

from invoicebot.fetchers.browser.field_match import fold
from invoicebot.fetchers.browser.probe_controls import PageForm, read_page
from invoicebot.fetchers.browser.probe_protocol import (
    FORM_PATH_PATTERN,
    CaptchaKind,
    ProbedField,
    ProbeOutcome,
)
from invoicebot.net.taught_url import without_session

#: How many landing pages the probe follows before it gives up (D193).
MAX_LANDING_CLICKS = 3
#: How long a page may take to render its form before the reading is final (D193).
SETTLE_MS = 2000
#: How often the page is read while it settles (D193).
SETTLE_POLL_MS = 250
#: How many landing candidates one page may hand back (D193).
MAX_LANDING_CANDIDATES = 200
#: How long a landing control's text or href may be (D193).
MAX_CANDIDATE_TEXT = 200
_MS_PER_SECOND = 1000
_HTTPS = "https"

#: Every visible control the probe may follow, in document order (D193).
_CANDIDATE_SELECTOR = (
    'a, button, input[type="submit"], input[type="button"], input[type="image"], [role="button"]'
)
#: Folded phrases that name a way into the form (D193). ``consulta`` covers ``consultar``.
_WANTED_PHRASES = ("comprobante", "consulta", "sin registro", "autenticacion datos")
#: Folded phrases that keep the probe off a control (D193).
_REFUSED_PHRASES = ("sesion", "eliminar", "pagar")
#: The one refused stem that ``sin registro`` removes first (D193).
_REGISTER_STEM = "registr"
_SIN_REGISTRO = "sin registro"

_RawText = Annotated[str, Field(max_length=MAX_CANDIDATE_TEXT)]


class _RawCandidate(BaseModel):
    """One visible control that may lead to the form, as the page reports it (D193)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    text: _RawText
    tag: _RawText
    raw_href: _RawText | None
    href: _RawText
    would_submit: bool


class _RawLanding(BaseModel):
    """Every landing candidate one script run reads from the main frame (D193)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidates: tuple[_RawCandidate, ...] = Field(max_length=MAX_LANDING_CANDIDATES)


#: One evaluate over the main frame; every value it returns is capped here (D193).
_READ_LANDING_JS = Template(
    """
() => {
  const selector = `$selector`;
  const maxCandidates = $max_candidates;
  const maxText = $max_text;
  const cut = (value) => String(value == null ? "" : value).slice(0, maxText);
  const isVisible = (element) =>
    element.getClientRects().length > 0 &&
    window.getComputedStyle(element).visibility === "visible";
  const textTypes = new Set(["text", "search", "tel", "number", "date", "email"]);
  const isTextLike = (element) => {
    if (element.tagName === "SELECT" || element.tagName === "TEXTAREA") {
      return true;
    }
    if (element.tagName !== "INPUT") {
      return false;
    }
    const kind = (element.getAttribute("type") || "").toLowerCase();
    return kind === "" || textTypes.has(kind);
  };
  const wouldSubmit = (element) => {
    const kind = (element.getAttribute("type") || "").toLowerCase();
    const submits =
      (element.tagName === "BUTTON" && (kind === "" || kind === "submit")) ||
      (element.tagName === "INPUT" && (kind === "submit" || kind === "image"));
    if (!submits || !element.form) {
      return false;
    }
    return Array.from(element.form.querySelectorAll("input, select, textarea")).some(
      (control) => !control.disabled && isVisible(control) && isTextLike(control)
    );
  };
  const candidates = [];
  const all = document.querySelectorAll(selector);
  for (let index = 0; index < all.length && candidates.length < maxCandidates; index++) {
    const element = all[index];
    if (element.disabled || !isVisible(element)) {
      continue;
    }
    const rawHref = element.getAttribute("href");
    const href = element.tagName === "A" ? String(element.href) : "";
    if (href.length > maxText && !/^javascript:/i.test(href)) {
      continue;
    }
    const text = element.tagName === "INPUT" ? element.value : element.textContent;
    candidates.push({
      index: index,
      text: cut(text),
      tag: cut(element.tagName),
      raw_href: rawHref === null ? null : cut(rawHref),
      href: cut(href),
      would_submit: wouldSubmit(element),
    });
  }
  return {candidates: candidates};
}
"""
).substitute(
    selector=_CANDIDATE_SELECTOR,
    max_candidates=MAX_LANDING_CANDIDATES,
    max_text=MAX_CANDIDATE_TEXT,
)


@dataclass(frozen=True)
class FormRead:
    """What reaching the portal ended with (D193).

    ``outcome`` is None when a form was found and may be filled; ``captcha`` is
    reported but not decided here. When ``outcome`` is set, the other fields are
    empty.
    """

    outcome: ProbeOutcome | None
    form_path: str | None = None
    captcha: CaptchaKind = "none"
    fields: tuple[ProbedField, ...] = ()
    submit: str | None = None


async def read_form(page: Page, url: str) -> FormRead:
    """Reach the portal's invoice form from *url*, following its menus (D193).

    The taught link is loaded first; a 429 or 5xx answer is ``busy`` and any other
    non-2xx ``technical``. From there the probe follows at most
    ``MAX_LANDING_CLICKS`` promising links or buttons on the approved host, never
    typing anything, until :func:`read_page` finds a form. A form with a visible
    password field is ``login_only``; a page or a link that leaves the approved
    host is ``other_host``.

    Raises:
        PlaywrightError: a page could not be loaded, read or clicked.
    """
    approved_host = _approved_host(url)
    outcome = _status_outcome(await page.goto(url))
    if outcome is not None:
        return FormRead(outcome=outcome)
    followed: set[tuple[str, str]] = set()
    reading = await _settle(page)
    clicks = 0
    while True:
        host_outcome = _page_host_outcome(page.url, approved_host)
        if host_outcome is not None:
            return FormRead(outcome=host_outcome)
        if reading.fields:
            return _found(page, reading)
        if clicks >= MAX_LANDING_CLICKS:
            return FormRead(outcome=_no_form_outcome(reading.has_password))
        candidates = await _landing_candidates(page, approved_host, followed)
        if not candidates:
            return FormRead(outcome=_no_form_outcome(reading.has_password))
        candidate = candidates[0]
        followed.add((candidate.text, candidate.href))
        outcome = await _follow(page, candidate)
        if outcome is not None:
            return FormRead(outcome=outcome)
        clicks += 1
        reading = await _settle(page)


async def _settle(page: Page) -> PageForm:
    """Read the page until it shows a form or ``SETTLE_MS`` runs out (D193).

    A read that fails because the page is navigating is retried until the deadline;
    a timeout is not retried. When a read succeeded, its last result is used; when
    none ever did, the error propagates.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + SETTLE_MS / _MS_PER_SECOND
    last: PageForm | None = None
    failure: PlaywrightError | None = None
    while True:
        try:
            reading = await read_page(page)
        except PlaywrightTimeoutError:
            raise
        except PlaywrightError as error:
            failure = error
            reading = None
        if reading is not None:
            last = reading
            if reading.fields:
                return reading
        now = loop.time()
        if now >= deadline:
            break
        wait = min(SETTLE_POLL_MS, (deadline - now) * _MS_PER_SECOND)
        await asyncio.sleep(wait / _MS_PER_SECOND)
    if last is not None:
        return last
    if failure is None:
        failure = PlaywrightError("the page could not be read")
    raise failure


async def _landing_candidates(
    page: Page, approved_host: str, followed: set[tuple[str, str]]
) -> list[_RawCandidate]:
    """Return the page's promising landing controls, in document order (D193)."""
    raw = _RawLanding.model_validate(await page.evaluate(_READ_LANDING_JS))
    return [
        candidate
        for candidate in raw.candidates
        if _is_landing(candidate, approved_host) and (candidate.text, candidate.href) not in followed
    ]


def _is_landing(candidate: _RawCandidate, approved_host: str) -> bool:
    """Return whether the candidate may lead to the form and may be followed (D193)."""
    if candidate.would_submit:
        return False
    if not _wants_landing(candidate.text):
        return False
    return _link_allowed(candidate, approved_host)


def _wants_landing(text: str) -> bool:
    """Return whether the control's text names a way into the form (D193).

    A wanted phrase is not enough when the text also says ``sesion``, ``eliminar``,
    ``pagar`` or ``registr``; ``sin registro`` is removed first, so it never counts
    as the register stem.
    """
    folded = " ".join(fold(text).split())
    if not any(phrase in folded for phrase in _WANTED_PHRASES):
        return False
    without_registration = folded.replace(_SIN_REGISTRO, " ")
    if any(phrase in without_registration for phrase in _REFUSED_PHRASES):
        return False
    return _REGISTER_STEM not in without_registration


def _link_allowed(candidate: _RawCandidate, approved_host: str) -> bool:
    """Return whether the control stays on the approved host or needs no address (D193)."""
    raw_href = (candidate.raw_href or "").strip()
    if _is_click_target(raw_href):
        return True
    parts = urlsplit(candidate.href)
    if parts.scheme.lower() != _HTTPS:
        return False
    return _is_approved_host((parts.hostname or "").lower(), approved_host)


def _is_click_target(raw_href: str) -> bool:
    """Return whether following the control needs no approved https address (D193)."""
    return raw_href == "" or raw_href.startswith("#") or raw_href.casefold().startswith("javascript:")


def _is_click(candidate: _RawCandidate) -> bool:
    """Return whether the candidate is clicked rather than navigated to (D193)."""
    return _is_click_target((candidate.raw_href or "").strip())


async def _follow(page: Page, candidate: _RawCandidate) -> ProbeOutcome | None:
    """Click the candidate or open its approved link, reporting a bad answer (D193).

    A same-page ``#`` or ``javascript:`` link is clicked so its script runs; an
    https link, including one with ``target=_blank``, is opened in this page.
    """
    if _is_click(candidate):
        await page.locator(_CANDIDATE_SELECTOR).nth(candidate.index).click()
        return None
    return _status_outcome(await page.goto(candidate.href))


def _found(page: Page, reading: PageForm) -> FormRead:
    """Turn a settled page reading into the probe's form result (D193)."""
    if reading.has_password:
        return FormRead(outcome=ProbeOutcome.LOGIN_ONLY)
    return FormRead(
        outcome=None,
        form_path=_form_path(page.url),
        captcha=reading.captcha,
        fields=reading.fields,
        submit=reading.submit,
    )


def _no_form_outcome(has_password: bool) -> ProbeOutcome:
    """Return ``login_only`` for a page with a password field, else ``no_form`` (D193)."""
    return ProbeOutcome.LOGIN_ONLY if has_password else ProbeOutcome.NO_FORM


def _approved_host(url: str) -> str:
    """Return the lowercase host of the link the sender taught (D193)."""
    return (urlsplit(url).hostname or "").lower()


def _page_host_outcome(page_url: str, approved_host: str) -> ProbeOutcome | None:
    """Return ``other_host`` unless the page is https on the approved host (D193)."""
    parts = urlsplit(page_url)
    if parts.scheme.lower() != _HTTPS:
        return ProbeOutcome.OTHER_HOST
    host = (parts.hostname or "").lower()
    if _is_approved_host(host, approved_host):
        return None
    return ProbeOutcome.OTHER_HOST


def _is_approved_host(host: str, approved_host: str) -> bool:
    """Return whether *host* is the approved host or a subdomain of it (D193)."""
    return host == approved_host or host.endswith(f".{approved_host}")


def _status_outcome(response: Response | None) -> ProbeOutcome | None:
    """Return the outcome of a navigation answer, None when it is 2xx (D193)."""
    if response is None:
        return ProbeOutcome.TECHNICAL
    status = response.status
    if 200 <= status < 300:
        return None
    if status == 429 or 500 <= status < 600:
        return ProbeOutcome.BUSY
    return ProbeOutcome.TECHNICAL


def _form_path(page_url: str) -> str | None:
    """Return the page's path without its session address, when the wire format takes it (D193)."""
    path = urlsplit(without_session(page_url)).path
    return path if re.fullmatch(FORM_PATH_PATTERN, path) else None
