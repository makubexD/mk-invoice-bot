"""Render an outgoing mail's plain-text body as the D197 HTML body.

Shared by the Graph mailer and the ``.eml`` builder the SMTP mailer sends (D197, D205),
so neither depends on the other.
"""

from __future__ import annotations

import html
import re

#: Inline style of the one wrapper div around the body (D197).
BODY_STYLE = "font-family: Calibri, Arial, sans-serif; font-size: 11pt"

_BLANK_LINES = re.compile(r"\n\s*\n")


def html_body(text: str) -> str:
    """Render plain ``text`` as an HTML body: escaped, one ``<p>`` per paragraph (D197)."""
    escaped = html.escape(text).replace("\r\n", "\n")
    paragraphs = [part.strip("\n") for part in _BLANK_LINES.split(escaped)]
    rendered = "".join(f"<p>{part.replace(chr(10), '<br>')}</p>" for part in paragraphs if part.strip())
    return f'<html><body><div style="{BODY_STYLE}">{rendered}</div></body></html>'
