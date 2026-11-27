"""Replay a saved form with the sender's captcha answer and read the result (D209)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import Locator, Page, Route

from invoicebot.config.models import MediaMaxBytes, Portal, PortalCaptcha
from invoicebot.fetchers.base import FetchReason, FetchResult
from invoicebot.fetchers.browser.form import (
    TechnicalFailure,
    download_selectors,
    empty_result,
    guard_attempt,
    read_result,
)
from invoicebot.fetchers.browser.protocol import CaptchaResume

_FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
_ANY_URL = "**/*"


async def resume_captcha(
    page: Page,
    portal: Portal,
    resume: CaptchaResume,
    out_dir: Path,
    max_bytes: MediaMaxBytes,
) -> FetchResult:
    """Post the saved form with the answer and classify the page that comes back (D209).

    Returns ``not_found_yet`` when the portal's not-found marker shows, the files when a
    download control shows, and ``captcha_wrong`` when only the captcha input shows again.
    """
    captcha = portal.captcha
    if not isinstance(captcha, PortalCaptcha):
        return empty_result(portal, FetchReason.TECHNICAL)
    saved: list[Path] = []

    async def attempt() -> FetchResult:
        answered = await _classify(page, portal, captcha, resume)
        return answered or await read_result(page, portal, out_dir, max_bytes, saved)

    return await guard_attempt(portal, saved, attempt)


async def _classify(
    page: Page, portal: Portal, captcha: PortalCaptcha, resume: CaptchaResume
) -> FetchResult | None:
    """Submit the form and judge the answer page; ``None`` means the download controls are there."""
    await _submit(page, resume)
    not_found = None if portal.not_found is None else page.locator(portal.not_found)
    downloads = [page.locator(selector) for _, selector in download_selectors(portal)]
    if not downloads:
        raise TechnicalFailure
    await _first_visible(downloads, not_found, page.locator(captcha.input))
    if not_found is not None and await not_found.is_visible():
        return empty_result(portal, FetchReason.NOT_FOUND_YET)
    if any([await control.count() > 0 for control in downloads]):
        return None
    return empty_result(portal, FetchReason.CAPTCHA_WRONG)


async def _first_visible(downloads: list[Locator], not_found: Locator | None, captcha_input: Locator) -> None:
    """Wait until any download control, the not-found marker or the captcha input is visible."""
    either = captcha_input if not_found is None else captcha_input.or_(not_found)
    for control in downloads:
        either = either.or_(control)
    await either.first.wait_for(state="visible")


async def _submit(page: Page, resume: CaptchaResume) -> None:
    """Go to the form's action as one POST of the saved fields, the answer and the submit button."""
    form = resume.form
    body = urlencode(
        [*form.fields, (form.captcha_name, resume.answer), (form.submit_name, form.submit_value)]
    )
    posted = False

    async def post_first_navigation(route: Route) -> None:
        nonlocal posted
        if posted or not route.request.is_navigation_request():
            await route.fallback()
            return
        posted = True
        headers = {**route.request.headers, "content-type": _FORM_CONTENT_TYPE}
        await route.fallback(method="POST", post_data=body, headers=headers)

    # Every URL, not only the action URL: the first navigation is this goto, and Chromium may
    # normalize the URL (PEBSA's session path), which an exact match would miss, silently
    # turning the POST into a GET of a fresh form.
    await page.route(_ANY_URL, post_first_navigation)
    try:
        await page.goto(form.action_url)
    finally:
        await page.unroute(_ANY_URL, post_first_navigation)
