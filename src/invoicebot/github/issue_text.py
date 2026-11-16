"""Gap issue text built only from typed fields (S8.11, R-11.12).

A gap issue goes to the private ops repo, so its title and body may carry only
the public or technical facts of spec 06 §3: the issuer RUC, the store name,
the CPE type, the fetcher reason codes and the approved portal URL. Every value
is re-rendered through an allow-list; anything else is dropped, so OCR text,
sender text, mentions, markdown and control characters never reach GitHub.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from urllib.parse import urlsplit

from invoicebot.fetchers.base import FetchReason
from invoicebot.rules.messages_es import KIND_DESC, KIND_SUBJECT

#: A RUC is the 11-digit issuer document (R-11.12).
_RUC_DIGITS = 11
#: R-11.12 caps the business name at 80 characters.
_NAME_MAX = 80

_NAME_PUNCTUATION = frozenset(" .,&-")
_TOKEN_PUNCTUATION = frozenset("-_.")
_MARKER_PUNCTUATION = frozenset("-_:")

_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(/[A-Za-z0-9._~/-]*)?$")


@dataclass(frozen=True)
class IssueFields:
    """The typed facts a gap issue may show (R-11.12)."""

    kind: str
    fingerprint: str
    ruc_emisor: int | None
    razon_social: str | None
    tipo: str | None
    reasons: tuple[FetchReason, ...]
    portal_key: str | None
    hint_url: str | None


def render_issue(gap: IssueFields) -> tuple[str, str]:
    """Render the title and body of a gap issue from typed fields only (R-11.12).

    Returns:
        The title ``gap: <kind> <subject>`` and a Markdown body that ends with
        the hidden ``<!-- gap:<fingerprint> -->`` marker D117 uses to deduplicate.
    """
    subject = _subject(gap)
    title = f"gap: {gap.kind} {subject}" if subject else f"gap: {gap.kind}"

    sections = [f"**{KIND_DESC[gap.kind]}**"]
    details = _details(gap)
    if details:
        sections.append("\n".join(details))
    url = _safe_url(gap.hint_url)
    if url:
        sections.append(f"```\n{url}\n```")
    sections.append(issue_marker(gap.fingerprint))
    return title, "\n\n".join(sections)


def issue_marker(fingerprint: str) -> str:
    """Return the hidden marker that identifies the issue for ``fingerprint`` (R-11.12, D117)."""
    return f"<!-- gap:{_marker(fingerprint)} -->"


def render_hint_comment(hint_url: str) -> str | None:
    """Render a taught portal link as a code-fenced comment, or None when it fails R-11.12."""
    url = _safe_url(hint_url)
    if not url:
        return None
    return f"```\n{url}\n```"


def _subject(gap: IssueFields) -> str:
    """Return the kind's subject rendered from typed values, or ``""`` when one is unusable."""
    template = KIND_SUBJECT[gap.kind]
    values = _subject_values(gap)
    names = [name for _, name, _, _ in string.Formatter().parse(template) if name]
    if any(not values[name] for name in names):
        return ""
    return template.format_map(values)


def _subject_values(gap: IssueFields) -> dict[str, str]:
    """Return the values ``KIND_SUBJECT`` placeholders read, mostly from the fingerprint."""
    parts = gap.fingerprint.split(":")
    flow, sink = parts[1:3] if len(parts) >= 3 else ("", "")
    return {
        "ruc_emisor": _ruc(gap.ruc_emisor),
        "portal_key": _keep(gap.portal_key, _TOKEN_PUNCTUATION),
        "flow": _keep(flow, _TOKEN_PUNCTUATION),
        "sink": _keep(sink, _TOKEN_PUNCTUATION),
        "num_doc_adq": _ruc(int(parts[1])) if len(parts) == 2 and parts[1].isdecimal() else "",
    }


def _details(gap: IssueFields) -> list[str]:
    """Return the body's typed detail lines, omitting fields that sanitize to nothing."""
    ruc = _ruc(gap.ruc_emisor)
    name = _keep(gap.razon_social, _NAME_PUNCTUATION)[:_NAME_MAX].strip()
    tipo = _keep(gap.tipo, _TOKEN_PUNCTUATION)
    lines = []
    if ruc:
        lines.append(f"- RUC emisor: {ruc}")
    if name:
        lines.append(f"- Razón social: {name}")
    if tipo:
        lines.append(f"- Tipo: {tipo}")
    if gap.reasons:
        lines.append("- Razones: " + ", ".join(reason.value for reason in gap.reasons))
    return lines


def _ruc(value: int | None) -> str:
    """Return the 11-digit RUC as text, or ``""`` when it is not exactly 11 digits (R-11.12)."""
    if value is None:
        return ""
    text = str(value)
    return text if len(text) == _RUC_DIGITS and text.isascii() and text.isdigit() else ""


def _keep(value: str | None, allowed: frozenset[str]) -> str:
    """Return ``value`` with every character outside ``allowed`` removed (R-11.12)."""
    if not value:
        return ""
    return "".join(char for char in value if char.isalnum() or char in allowed)


def _marker(fingerprint: str) -> str:
    """Return the fingerprint as it may appear inside an HTML comment (R-11.12)."""
    return _keep(fingerprint, _MARKER_PUNCTUATION)


def _safe_url(value: str | None) -> str:
    """Return the URL reduced to ``https://host/path`` when it passes R-11.12, else ``""``."""
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme != "https" or not host:
        return ""
    reduced = f"https://{host}{parts.path}"
    return reduced if _URL_RE.fullmatch(reduced) else ""
