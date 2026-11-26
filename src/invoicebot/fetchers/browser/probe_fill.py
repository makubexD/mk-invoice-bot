"""Fill the portal's form once, submit it once and save what it offers (D193).

:func:`probe` reads the form with :func:`read_form`, decides every value before typing
(:func:`plan_fills`), then types, submits and downloads the ``pdf`` and ``xml`` the
result page links. While it works, every non-GET request to a host other than the
approved one is aborted. No invoice value is put in the report, a log or an exception.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import Download, Page, Route
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from pydantic import BaseModel, ConfigDict, Field

from invoicebot.fetchers.browser.field_match import fold
from invoicebot.fetchers.browser.form import file_sha256
from invoicebot.fetchers.browser.probe_form import FormRead, approved_host_of, is_approved_host, read_form
from invoicebot.fetchers.browser.probe_protocol import (
    SELECTOR_PATTERN,
    ProbedField,
    ProbeOutcome,
    ProbeReport,
    ProbeRequest,
    safe_text,
)
from invoicebot.fetchers.browser.probe_values import apply_fills, plan_fills
from invoicebot.fetchers.browser.protocol import HelperFile, error_class_of

#: How often the result page is read while it settles (D193).
_POLL_MS = 200
_NOT_FOUND_PHRASES = ("no se encontr", "no existe")
_CONTROLS = 'a, button, input[type="submit"], input[type="button"], [role="button"]'
_Kind = Literal["pdf", "xml"]
_KINDS: tuple[_Kind, ...] = ("pdf", "xml")
#: A download named for the other kind is a stray of the previous click (as in form.py).
_OTHER_SUFFIX: dict[_Kind, str] = {"pdf": ".xml", "xml": ".pdf"}
_MAX_RESULT_TEXT = 20000
_MAX_RESULT_CONTROLS = 200
_MAX_CONTROL_TEXT = 200

#: Reads the result page: the page text and each visible control naming pdf or xml (D193).
_READ_RESULT_JS = """(selector) => {
  const visible = (el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const controls = [...document.querySelectorAll(selector)].map((el, index) => ({
    index, id: el.id || '', name: el.getAttribute('name') || '',
    text: (el.innerText || el.value || '').slice(0, 200),
    href: (el.getAttribute('href') || '').slice(0, 200),
    shown: visible(el),
  })).filter((c) => c.shown && /pdf|xml/i.test(c.text + ' ' + c.href)).slice(0, 200)
    .map(({index, id, name, text, href}) => ({
      index, id: id.slice(0, 200), name: name.slice(0, 200), text, href,
    }));
  return {text: (document.body ? document.body.innerText : '').slice(0, 20000), controls};
}"""


class _RawResultControl(BaseModel):
    """One control the result page script reported; page data, so untrusted (D193)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    id: str = Field(max_length=_MAX_CONTROL_TEXT)
    name: str = Field(max_length=_MAX_CONTROL_TEXT)
    text: str = Field(max_length=_MAX_CONTROL_TEXT)
    href: str = Field(max_length=_MAX_CONTROL_TEXT)


class _RawResult(BaseModel):
    """What the result page script reported; page data, so untrusted (D193)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(max_length=_MAX_RESULT_TEXT)
    controls: tuple[_RawResultControl, ...] = Field(max_length=_MAX_RESULT_CONTROLS)


class _TooBig(Exception):
    """A downloaded file passed the size limit."""


@dataclass(frozen=True)
class _Link:
    """A visible control that offers one file (D193)."""

    index: int
    control_id: str
    name: str


@dataclass(frozen=True)
class _Result:
    """What the page shows after the submit (D193)."""

    not_found: bool
    links: dict[_Kind, _Link]


@dataclass
class _Run:
    """What one probe has learned so far, for the report on any ending (D193)."""

    form: FormRead | None = None
    saved: list[Path] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


async def probe(page: Page, request: ProbeRequest) -> ProbeReport:
    """Reach the form, fill it, submit once and download the pdf and xml (D193).

    Every ``PlaywrightTimeoutError`` is ``busy`` before the form is found and
    ``no_download`` after; any other ``PlaywrightError`` is ``technical``. A report that
    is not ``ok`` has removed the files it saved.
    """
    run = _Run()
    try:
        report = await _run(page, request, run)
    except _TooBig:
        report = _report(run, ProbeOutcome.TECHNICAL)
    except PlaywrightTimeoutError:
        report = _report(run, ProbeOutcome.BUSY if run.form is None else ProbeOutcome.NO_DOWNLOAD)
    except PlaywrightError as error:
        # A navigation our own guard aborted fails the click: the portal sent nothing.
        if run.blocked:
            report = _report(run, ProbeOutcome.NO_DOWNLOAD)
        else:
            report = _report(run, ProbeOutcome.TECHNICAL, error_class=error_class_of(error))
    if report.outcome is not ProbeOutcome.OK:
        for path in run.saved:
            path.unlink(missing_ok=True)
        report = report.model_copy(update={"files": (), "pdf": None, "xml": None})
    return report


async def _run(page: Page, request: ProbeRequest, run: _Run) -> ProbeReport:
    """Read, decide, fill, submit and download; the report for the way it ended (D193)."""
    form = await read_form(page, request.url)
    run.form = form
    stop = _stop_outcome(form)
    if stop is not None:
        return _stopped(run, stop, form.unmatched)
    plan = await plan_fills(page, form.fields, request.invoice)
    if plan.blocked is not None:
        return _stopped(run, ProbeOutcome.UNMATCHED_FIELD, _field_name(plan.blocked))
    await _guard(page, approved_host_of(request.url), run.blocked)
    await apply_fills(page, plan.fills)
    await page.locator(str(form.submit)).click()
    result = await _wait_result(page, request.page_timeout_ms)
    if result is None:
        return _report(run, ProbeOutcome.NO_DOWNLOAD)
    if result.not_found and not result.links:
        return _report(run, ProbeOutcome.NOT_FOUND)
    return await _download(page, request, run, result)


def _stop_outcome(form: FormRead) -> ProbeOutcome | None:
    """Return why the form must not be filled, or None when it may be (D193)."""
    if form.outcome is not None:
        return form.outcome
    if form.captcha != "none":
        return ProbeOutcome.CAPTCHA
    if form.required_unmatched:
        return ProbeOutcome.UNMATCHED_FIELD
    if form.submit is None:
        return ProbeOutcome.NO_SUBMIT
    return None


def _field_name(field: ProbedField) -> str:
    """Return a field's label, else its selector as safe text, to name it in the report (D193)."""
    return field.label or safe_text(field.selector)


def _report(
    run: _Run,
    outcome: ProbeOutcome,
    *,
    error_class: str | None = None,
    files: tuple[HelperFile, ...] = (),
    pdf: str | None = None,
    xml: str | None = None,
) -> ProbeReport:
    """Build the report with what is known of the form, and no invoice value (D193)."""
    form = run.form
    if form is None or form.outcome is not None:
        return ProbeReport(outcome=outcome, error_class=error_class)
    return ProbeReport(
        outcome=outcome,
        form_path=form.form_path,
        captcha=form.captcha,
        fields=form.fields,
        submit=form.submit,
        pdf=pdf,
        xml=xml,
        files=files,
        error_class=error_class,
    )


def _stopped(run: _Run, outcome: ProbeOutcome, field_name: str | None) -> ProbeReport:
    """Build the report of a probe stopped before typing; ``unmatched_field`` names its field (D193)."""
    report = _report(run, outcome)
    if outcome is not ProbeOutcome.UNMATCHED_FIELD or not field_name:
        return report
    return ProbeReport.model_validate(report.model_dump() | {"stopped_at": field_name})


async def _guard(page: Page, approved_host: str, blocked: list[str]) -> None:
    """Abort every non-GET request to a host other than the approved one (D193).

    Each abort is noted in *blocked* by method only, never by URL.
    """

    async def handle(route: Route) -> None:
        request = route.request
        host = (urlsplit(request.url).hostname or "").lower()
        if request.method != "GET" and not is_approved_host(host, approved_host):
            blocked.append(request.method)
            await route.abort()
        else:
            await route.fallback()

    await page.route("**/*", handle)


async def _wait_result(page: Page, timeout_ms: int) -> _Result | None:
    """Poll the result page until a file link or a not-found text shows, else None (D193)."""
    await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    for _ in range(max(1, timeout_ms // _POLL_MS)):
        result = await _read_result(page)
        if result.links or result.not_found:
            return result
        await page.wait_for_timeout(_POLL_MS)
    return None


async def _read_result(page: Page) -> _Result:
    """Read the page's not-found text and the first control offering each file (D193)."""
    raw = _RawResult.model_validate(await page.evaluate(_READ_RESULT_JS, _CONTROLS))
    text = fold(raw.text)
    links: dict[_Kind, _Link] = {}
    for control in raw.controls:
        haystack = f"{control.text} {control.href}".casefold()
        for kind in _KINDS:
            if kind in haystack and kind not in links:
                links[kind] = _Link(control.index, control.id, control.name)
    return _Result(any(phrase in text for phrase in _NOT_FOUND_PHRASES), links)


async def _download(page: Page, request: ProbeRequest, run: _Run, result: _Result) -> ProbeReport:
    """Save each offered file; ``ok`` when at least one arrived (D193)."""
    files: list[HelperFile] = []
    selectors: dict[_Kind, str | None] = {"pdf": None, "xml": None}
    for kind, link in result.links.items():
        selectors[kind] = await _selector(page, link)
        saved = await _save(page, request, run, kind, link)
        if saved is not None:
            files.append(saved)
    if not files:
        return _report(run, ProbeOutcome.NO_DOWNLOAD)
    return _report(run, ProbeOutcome.OK, files=tuple(files), pdf=selectors["pdf"], xml=selectors["xml"])


async def _save(page: Page, request: ProbeRequest, run: _Run, kind: _Kind, link: _Link) -> HelperFile | None:
    """Click one link and save its download; None when no download starts (D193).

    Raises:
        _TooBig: the file is over the limit for its kind.
    """

    def is_this_kind(download: Download) -> bool:
        return Path(download.suggested_filename).suffix.lower() != _OTHER_SUFFIX[kind]

    try:
        async with page.expect_download(predicate=is_this_kind, timeout=request.page_timeout_ms) as info:
            await page.locator(_CONTROLS).nth(link.index).click()
        download = await info.value
    except PlaywrightTimeoutError:
        return None
    path = request.out_dir / kind
    run.saved.append(path)
    await download.save_as(path)
    size = path.stat().st_size
    if size > (request.max_bytes.pdf if kind == "pdf" else request.max_bytes.xml):
        raise _TooBig
    return HelperFile(kind=kind, path=path, size=size, sha256=file_sha256(path))


async def _selector(page: Page, link: _Link) -> str | None:
    """Return ``#id`` or ``[name=...]`` when exactly one element matches, else None (D193)."""
    candidates = (f"#{link.control_id}", f"[name='{link.name}']")
    for candidate in candidates:
        if re.fullmatch(SELECTOR_PATTERN, candidate) and await page.locator(candidate).count() == 1:
            return candidate
    return None
