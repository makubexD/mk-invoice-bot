"""Fill a generic portal form and download an invoice's files (S10.2, R-5.8).

The portal file drives everything: each field's template is rendered over the
invoice's values, then typed, selected, picked or clicked. Expected outcomes
come back as reason codes; Playwright errors never escape this module.
"""

from __future__ import annotations

import hashlib
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from invoicebot.config.models import (
    CalendarField,
    MediaMaxBytes,
    PickField,
    Portal,
    SelectField,
)
from invoicebot.fetchers.base import FetchedFile, FetchInvoice, FetchReason, FetchResult

type DownloadKind = Literal["pdf", "xml"]

_FORMATTER = string.Formatter()
_ALLOWED_NAMES = frozenset({"ruc_emisor", "tipo", "tipo_label", "serie", "numero", "fecha", "total"})
_CALENDAR_STEPS = 24
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class FieldAction:
    """One rendered form action (S10.2).

    ``selector`` locates the field, drawn from the portal file's key. ``value``
    is the rendered text, option, option selector or calendar cell selector;
    ``prev`` is the calendar's previous-month button (not rendered).
    """

    selector: str
    kind: Literal["fill", "select", "pick", "calendar"]
    value: str
    prev: str | None = None


class _Technical(Exception):
    """An expected technical failure inside one fetch attempt."""


def render_fields(portal: Portal, invoice: FetchInvoice) -> list[FieldAction]:
    """Render every portal field template into an action, in the file's order.

    Raises:
        ValueError: a template names a value outside the invoice fields, uses
            attribute or item access, or needs ``tipo_label`` for a tipo the
            portal has no label for.
    """
    values = _values(portal, invoice)
    actions: list[FieldAction] = []
    for selector, field in portal.fields.items():
        if isinstance(field, SelectField):
            actions.append(FieldAction(selector, "select", _render(field.select, values)))
        elif isinstance(field, PickField):
            actions.append(FieldAction(selector, "pick", _render(field.pick, values)))
        elif isinstance(field, CalendarField):
            actions.append(
                FieldAction(
                    selector,
                    "calendar",
                    _render(field.calendar.cell, values),
                    prev=field.calendar.prev,
                )
            )
        else:
            actions.append(FieldAction(selector, "fill", _render(field, values)))
    return actions


async def fill_and_download(
    page: Page,
    portal: Portal,
    invoice: FetchInvoice,
    out_dir: Path,
    max_bytes: MediaMaxBytes,
) -> FetchResult:
    """Fill the portal's form, submit it and save the invoice's files (R-5.8).

    A technical or timed-out attempt removes the files it had already saved,
    so ``out_dir`` holds a file only when the attempt succeeds.
    """
    try:
        actions = render_fields(portal, invoice)
    except ValueError:
        return _result(portal, FetchReason.TECHNICAL)
    saved: list[Path] = []
    try:
        return await _fetch(page, portal, actions, out_dir, max_bytes, saved)
    except _Technical:
        _remove(saved)
        return _result(portal, FetchReason.TECHNICAL)
    except PlaywrightTimeoutError:
        _remove(saved)
        return _result(portal, FetchReason.TIMEOUT)
    except PlaywrightError:
        _remove(saved)
        return _result(portal, FetchReason.TECHNICAL)


async def _fetch(
    page: Page,
    portal: Portal,
    actions: list[FieldAction],
    out_dir: Path,
    max_bytes: MediaMaxBytes,
    saved: list[Path],
) -> FetchResult:
    """Go to the portal, perform the actions, and save what it downloads."""
    if portal.url is None:
        raise _Technical
    await page.goto(portal.url)
    for action in actions:
        await _perform(page, action)
    if portal.submit is None:
        raise _Technical
    await page.locator(portal.submit).click()

    selectors = _download_selectors(portal)
    if not selectors:
        raise _Technical
    if await _wait_for_result(page, portal, selectors[0][1]):
        return _result(portal, FetchReason.NOT_FOUND_YET)
    files = [
        await _download_one(page, kind, selector, out_dir, max_bytes, saved)
        for kind, selector in selectors
        if await page.locator(selector).count() > 0
    ]
    if len(files) != len(selectors):
        raise _Technical
    return FetchResult(reason=FetchReason.OK, files=tuple(files), portal_key=portal.key)


def _download_selectors(portal: Portal) -> list[tuple[DownloadKind, str]]:
    """The download controls the portal file names, in pdf-then-xml order."""
    if portal.downloads is None:
        return []
    selectors: list[tuple[DownloadKind, str]] = []
    if portal.downloads.pdf is not None:
        selectors.append(("pdf", portal.downloads.pdf))
    if portal.downloads.xml is not None:
        selectors.append(("xml", portal.downloads.xml))
    return selectors


async def _wait_for_result(page: Page, portal: Portal, first_download: str) -> bool:
    """Wait for a download control or the not-found marker; ``True`` means not found."""
    download = page.locator(first_download)
    if portal.not_found is None:
        await download.first.wait_for(state="visible")
        return False
    not_found = page.locator(portal.not_found)
    await download.or_(not_found).first.wait_for(state="visible")
    return await not_found.is_visible()


async def _perform(page: Page, action: FieldAction) -> None:
    """Perform one rendered field action on the page."""
    locator = page.locator(action.selector)
    match action.kind:
        case "fill":
            await locator.fill(action.value)
        case "select":
            await locator.select_option(action.value)
        case "pick":
            await locator.click()
            await page.locator(action.value).click()
        case "calendar":
            await _calendar(page, action)


async def _calendar(page: Page, action: FieldAction) -> None:
    """Open the date picker and step back until the invoice's day shows, then click it."""
    if action.prev is None:
        raise _Technical
    await page.locator(action.selector).click()
    cell = page.locator(action.value)
    for _ in range(_CALENDAR_STEPS):
        if await cell.count() > 0:
            await cell.click()
            return
        await page.locator(action.prev).click()
    raise _Technical


async def _download_one(
    page: Page,
    kind: DownloadKind,
    selector: str,
    out_dir: Path,
    max_bytes: MediaMaxBytes,
    saved: list[Path],
) -> FetchedFile:
    """Click one download control, save the file, and reject it if it is too big."""
    async with page.expect_download() as info:
        await page.locator(selector).click()
    download = await info.value
    path = out_dir / kind
    saved.append(path)
    await download.save_as(path)
    size = path.stat().st_size
    if size > _limit(max_bytes, kind):
        raise _Technical
    return FetchedFile(kind=kind, path=path, size=size, sha256=_sha256(path))


def _limit(max_bytes: MediaMaxBytes, kind: DownloadKind) -> int:
    """The most bytes the kind's limit allows."""
    return max_bytes.pdf if kind == "pdf" else max_bytes.xml


def _sha256(path: Path) -> str:
    """The SHA-256 hex digest of a file, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove(paths: list[Path]) -> None:
    """Delete the files this attempt saved."""
    for path in paths:
        path.unlink(missing_ok=True)


def _result(portal: Portal, reason: FetchReason) -> FetchResult:
    """A result with no files, always keyed to this portal."""
    return FetchResult(reason=reason, files=(), portal_key=portal.key)


def _values(portal: Portal, invoice: FetchInvoice) -> dict[str, object]:
    """The values a field template may format, including the tipo's label."""
    values: dict[str, object] = {
        "ruc_emisor": invoice.ruc_emisor,
        "tipo": invoice.tipo,
        "serie": invoice.serie,
        "numero": invoice.numero,
        "fecha": invoice.fecha,
        "total": invoice.total,
    }
    label = portal.tipo_labels.get(invoice.tipo)
    if label is not None:
        values["tipo_label"] = label
    return values


def _render(template: str, values: dict[str, object]) -> str:
    """Render one template, rejecting names the portal may not use."""
    _check_template(template, template)
    try:
        return _FORMATTER.vformat(template, (), values)
    except KeyError:
        raise ValueError(
            f"portal field template {template!r} uses tipo_label for an unlabelled tipo"
        ) from None


def _check_template(template: str, source: str) -> None:
    """Reject a template (or a nested format spec) that names a disallowed value."""
    for _, field_name, format_spec, _ in _FORMATTER.parse(template):
        if field_name is not None and field_name not in _ALLOWED_NAMES:
            raise ValueError(f"portal field template {source!r} names disallowed field {field_name!r}")
        if format_spec is not None:
            _check_template(format_spec, source)
