"""Integration tests for the helper's captcha resume mode (D209)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import pytest
import pytest_asyncio
from playwright.async_api import Browser, Page, Route, async_playwright

from invoicebot.config.models import MediaMaxBytes, Portal
from invoicebot.fetchers.base import FetchReason
from invoicebot.fetchers.browser.captcha_resume import resume_captcha
from invoicebot.fetchers.browser.protocol import CaptchaForm, CaptchaResume

PDF_BYTES = b"%PDF-1.4 synthetic invoice\n"
XML_BYTES = b"<invoice><id>F001-123</id></invoice>\n"
MAX_BYTES = MediaMaxBytes(image=1024 * 1024, pdf=1024 * 1024, xml=1024 * 1024, zip=1024 * 1024)

_CAPTCHA_INPUT = '<input id="code" name="captcha" />'
_RESULTS = {
    "good": '<a id="pdf" href="/files/doc.pdf">PDF</a><a id="xml" href="/files/doc.xml">XML</a>',
    "bad": f'<form action="/consulta" method="post">{_CAPTCHA_INPUT}</form>',
    "gone": f'<div id="nf">No encontrado</div><form action="/consulta" method="post">{_CAPTCHA_INPUT}</form>',
}


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A headless Chromium for this module's tests."""
    async with async_playwright() as playwright:
        launched = await playwright.chromium.launch()
        yield launched
        await launched.close()


@pytest_asyncio.fixture(loop_scope="session")
async def page(browser: Browser) -> AsyncIterator[Page]:
    """A fresh page with a short timeout."""
    fresh = await browser.new_page(accept_downloads=True)
    fresh.set_default_timeout(1500)
    yield fresh
    await fresh.close()


def _portal() -> Portal:
    """A portal whose captcha the sender solves."""
    return Portal.model_validate(
        {
            "key": "test-portal",
            "adapter": "generic_form",
            "status": "active",
            "url": "https://portal.test/form",
            "captcha": {"image": "#img", "input": "#code", "solver": ["user"]},
            "submit": "#submit",
            "downloads": {"pdf": "#pdf", "xml": "#xml"},
            "not_found": "#nf",
        }
    )


def _resume(answer: str) -> CaptchaResume:
    """The saved form (as the ask step leaves it) and the sender's answer."""
    form = CaptchaForm(
        page_url="https://portal.test/form",
        action_url="https://portal.test/consulta",
        fields=(("token", "abc"), ("viewstate", "x y&z"), ("total", "118.50")),
        captcha_name="captcha",
        submit_name="accion",
        submit_value="Buscar",
    )
    return CaptchaResume(form=form, answer=answer)


async def _serve(page: Page, posts: list[tuple[str, str, str]]) -> None:
    """Serve the action URL: it records each request and answers with the page its answer picks."""

    async def handle(route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/consulta":
            body = request.post_data or ""
            posts.append((request.method, request.headers.get("content-type", ""), body))
            answer = dict(parse_qsl(body)).get("captcha", "bad")
            page_html = _RESULTS.get(answer, _RESULTS["bad"])
            await route.fulfill(status=200, body=page_html.encode(), content_type="text/html")
        elif path in {"/files/doc.pdf", "/files/doc.xml"}:
            is_pdf = path.endswith(".pdf")
            await route.fulfill(
                status=200,
                body=PDF_BYTES if is_pdf else XML_BYTES,
                headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Disposition": f'attachment; filename="{path.rsplit("/", 1)[1]}"',
                },
            )
        else:
            await route.fulfill(status=404, body=b"not found")

    await page.route("https://portal.test/**", handle)


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_with_the_right_answer_saves_the_files(page: Page, tmp_path: Path) -> None:
    """D209: the right answer returns the invoice's files and ok."""
    await _serve(page, [])

    result = await resume_captcha(page, _portal(), _resume("good"), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.OK
    assert {file.kind for file in result.files} == {"pdf", "xml"}
    assert (tmp_path / "pdf").read_bytes() == PDF_BYTES
    assert (tmp_path / "xml").read_bytes() == XML_BYTES


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_with_a_wrong_answer_is_captcha_wrong(page: Page, tmp_path: Path) -> None:
    """D209: a form served again with no download and no marker is captcha_wrong."""
    await _serve(page, [])

    result = await resume_captcha(page, _portal(), _resume("bad"), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.CAPTCHA_WRONG
    assert result.files == ()


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_posts_the_saved_fields_then_the_answer_then_the_submit(
    page: Page, tmp_path: Path
) -> None:
    """D209: one POST with the hidden fields unchanged and in order, the captcha, then the submit."""
    posts: list[tuple[str, str, str]] = []
    await _serve(page, posts)

    await resume_captcha(page, _portal(), _resume("good"), tmp_path, MAX_BYTES)

    assert len(posts) == 1
    method, content_type, body = posts[0]
    assert method == "POST"
    assert content_type == "application/x-www-form-urlencoded"
    assert parse_qsl(body) == [
        ("token", "abc"),
        ("viewstate", "x y&z"),
        ("total", "118.50"),
        ("captcha", "good"),
        ("accion", "Buscar"),
    ]


@pytest.mark.asyncio(loop_scope="session")
async def test_resume_not_found_wins_over_the_captcha_input(page: Page, tmp_path: Path) -> None:
    """D209: the not-found marker gives not_found_yet even when the captcha input shows again."""
    await _serve(page, [])

    result = await resume_captcha(page, _portal(), _resume("gone"), tmp_path, MAX_BYTES)

    assert result.reason is FetchReason.NOT_FOUND_YET
