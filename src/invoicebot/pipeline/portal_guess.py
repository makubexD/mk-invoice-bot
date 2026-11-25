"""Turn the strip under a ticket's ``CONSULT… EN`` line into the URL BR-F14 offers (D191).

The strip is OCR text, so its host may carry spaces or commas where a dot was printed, a
garbled scheme, and a misspelled path segment. The repair only proposes an address; it is
still checked by :func:`invoicebot.extract.fields.is_printed_url` before it is offered.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from invoicebot.extract.fields import is_printed_host, is_printed_url
from invoicebot.sunat_origins import is_sunat_host

#: How close (0..1) the repaired host must be to a known host to be replaced by it (D191).
HOST_MATCH_MIN_RATIO = 0.85
#: The shortest issuer-name word a path segment may be replaced by (D191).
NAME_WORD_MIN_CHARS = 3
#: How close (0..1) a path segment must be to an issuer-name word to be replaced (D191).
NAME_MATCH_MIN_RATIO = 0.75
#: The top levels a repaired portal URL may end in (D191).
ACCEPTED_TLDS = frozenset({"pe", "com", "net", "org"})

#: A leading garbled or real scheme, dropped before the host is read (D191).
_SCHEME_PATTERN = re.compile(r"^h\S{2,5}?[:;.,]*[/\\|]+")
#: The separators OCR leaves where a dot was printed, repaired to a dot (D191).
_HOST_SEPARATOR_PATTERN = re.compile(r"[\s,;]+")
#: The characters a host may keep (D191).
_HOST_CHARS_PATTERN = re.compile(r"[^a-z0-9.-]")
#: The repeated dots a repaired host collapses (D191).
_REPEATED_DOTS_PATTERN = re.compile(r"\.{2,}")
#: The characters a path segment may keep (D191).
_PATH_SEGMENT_CHARS_PATTERN = re.compile(r"[^a-z0-9._~-]")
#: One issuer-name word: a run of letters and digits (D191).
_NAME_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def repair_url(strip: str, issuer_name: str | None, known_hosts: frozenset[str]) -> str | None:
    """Return the portal URL the strip most likely means, or None when it is not one (BR-F14, D191)."""
    host_part, _, path_part = _SCHEME_PATTERN.sub("", strip.lower().strip()).partition("/")
    host = _repair_host(host_part, known_hosts)
    segments = _path_segments(path_part, issuer_name)
    suffix = f"/{'/'.join(segments)}" if segments else ""
    url = f"https://{host}{suffix}"
    return url if _is_portal_url(host, url) else None


def _repair_host(host_part: str, known_hosts: frozenset[str]) -> str:
    """Return the host with OCR separators repaired and the closest known host (D191)."""
    host = _HOST_SEPARATOR_PATTERN.sub(".", host_part)
    host = _REPEATED_DOTS_PATTERN.sub(".", _HOST_CHARS_PATTERN.sub("", host)).strip(".")
    return _closest_known_host(host, known_hosts)


def _closest_known_host(host: str, known_hosts: frozenset[str]) -> str:
    """Return the known host closest to ``host`` when close enough, else ``host`` (D191)."""
    best: tuple[float, str] | None = None
    for known in sorted(known_hosts):
        ratio = SequenceMatcher(None, host, known).ratio()
        if ratio >= HOST_MATCH_MIN_RATIO and (best is None or ratio > best[0]):
            best = (ratio, known)
    return host if best is None else best[1]


def _path_segments(path_part: str, issuer_name: str | None) -> list[str]:
    """Return the cleaned path segments, with the last repaired from the issuer name (D191)."""
    segments: list[str] = []
    for segment in path_part.split("/"):
        cleaned = _PATH_SEGMENT_CHARS_PATTERN.sub("", segment)
        if cleaned:
            segments.append(cleaned)
    if segments and issuer_name is not None:
        segments[-1] = _repair_segment(segments[-1], _issuer_words(issuer_name))
    return segments


def _issuer_words(issuer_name: str) -> tuple[str, ...]:
    """Return the issuer name's words that are long enough to be a path segment (D191)."""
    return tuple(
        word for word in _NAME_WORD_PATTERN.findall(issuer_name.lower()) if len(word) >= NAME_WORD_MIN_CHARS
    )


def _repair_segment(segment: str, words: tuple[str, ...]) -> str:
    """Return the issuer word closest to ``segment`` when close enough, else ``segment`` (D191)."""
    if segment in words:
        return segment
    best: tuple[float, str] | None = None
    for word in words:
        ratio = SequenceMatcher(None, segment, word).ratio()
        if ratio >= NAME_MATCH_MIN_RATIO and (best is None or ratio > best[0]):
            best = (ratio, word)
    return segment if best is None else best[1]


def _is_portal_url(host: str, url: str) -> bool:
    """Return whether ``url`` is an accepted portal address for ``host`` (D191)."""
    last_label = host.rsplit(".", maxsplit=1)[-1]
    return (
        is_printed_host(host)
        and last_label in ACCEPTED_TLDS
        and not is_sunat_host(host)
        and is_printed_url(url)
    )
