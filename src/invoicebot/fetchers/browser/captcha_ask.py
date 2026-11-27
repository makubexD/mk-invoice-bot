"""Fill a captcha portal's form and save the captcha and the form for a resume (D209)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from playwright.async_api import Page

from invoicebot.config.models import Portal, PortalCaptcha
from invoicebot.fetchers.base import FetchInvoice, FetchReason, FetchResult
from invoicebot.fetchers.browser.form import (
    FieldAction,
    TechnicalFailure,
    empty_result,
    fill_form,
    guard_attempt,
    render_fields,
)
from invoicebot.fetchers.browser.protocol import MAX_CAPTCHA_IMAGE_BYTES, CaptchaChallenge, CaptchaForm

CAPTCHA_FILE_NAME = "captcha.png"

#: Run on the captcha input: the form's resolved action, the page URL and its text fields,
#: without files, with the input's name and without the input's own value (the sender's answer replaces it).
_READ_FORM_SCRIPT = """(input) => {
  const form = input.form;
  if (form === null) return null;
  const fields = [];
  for (const [name, value] of new FormData(form)) {
    if (typeof value === "string" && name !== input.name) fields.push([name, value]);
  }
  return { action: form.action, page: location.href, captcha: input.name, fields };
}"""


async def ask_captcha(
    page: Page,
    portal: Portal,
    invoice: FetchInvoice,
    out_dir: Path,
) -> FetchResult | CaptchaChallenge:
    """Open the portal, fill the form and capture the captcha image and the form (D209).

    Returns the challenge, or a ``technical`` or ``timeout`` result when the portal cannot
    be asked; a failed attempt leaves no image in ``out_dir``.
    """
    captcha = portal.captcha
    if not isinstance(captcha, PortalCaptcha):
        return empty_result(portal, FetchReason.TECHNICAL)
    try:
        actions = render_fields(portal, invoice)
    except ValueError:
        return empty_result(portal, FetchReason.TECHNICAL)
    saved: list[Path] = []
    return await guard_attempt(portal, saved, lambda: _ask(page, portal, captcha, actions, out_dir, saved))


async def _ask(
    page: Page,
    portal: Portal,
    captcha: PortalCaptcha,
    actions: list[FieldAction],
    out_dir: Path,
    saved: list[Path],
) -> CaptchaChallenge:
    """Go to the portal, fill the form, then save the captcha image and read the form."""
    if portal.url is None or portal.submit is None:
        raise TechnicalFailure
    await page.goto(portal.url)
    await fill_form(page, actions)
    path = out_dir / CAPTCHA_FILE_NAME
    saved.append(path)
    await page.locator(captcha.image).screenshot(path=path, type="png")
    data = path.read_bytes()
    if len(data) > MAX_CAPTCHA_IMAGE_BYTES:
        raise TechnicalFailure
    form = await _read_form(page, portal.submit, captcha.input)
    try:
        return CaptchaChallenge(path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), form=form)
    except ValueError:
        raise TechnicalFailure from None


async def _read_form(page: Page, submit: str, captcha_input: str) -> CaptchaForm:
    """The form that holds the captcha input, with the submit button's name and value."""
    read = await page.locator(captcha_input).evaluate(_READ_FORM_SCRIPT)
    button = page.locator(submit)
    name = await button.get_attribute("name")
    if read is None or name is None:
        raise TechnicalFailure
    value = await button.get_attribute("value")
    try:
        return CaptchaForm(
            page_url=read["page"],
            action_url=read["action"],
            fields=tuple((field[0], field[1]) for field in read["fields"]),
            captcha_name=read["captcha"],
            submit_name=name,
            submit_value=value or "",
        )
    except ValueError:
        raise TechnicalFailure from None
