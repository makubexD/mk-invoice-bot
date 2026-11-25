"""Turn the strip under a ticket's ``CONSULT… EN`` line into the candidate URLs BR-F14 tries (D191, D192).

The strip is OCR text, so its host may carry spaces or commas where a dot was printed, a
garbled scheme, and a misspelled path segment. The candidates are tried before one is offered
(D192), and every one is still checked by :func:`invoicebot.extract.fields.is_printed_url`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from difflib import SequenceMatcher

from invoicebot.extract.fields import is_printed_host, is_printed_url
from invoicebot.sunat_origins import is_sunat_host

#: How close (0..1) a garbled scheme must be to ``https`` or ``http`` to be dropped (D192).
SCHEME_MATCH_MIN_RATIO = 0.6
#: How close (0..1) the repaired host must be to a known host to be replaced by it (D191).
HOST_MATCH_MIN_RATIO = 0.85
#: The shortest issuer-name word a path segment may be replaced by (D191).
NAME_WORD_MIN_CHARS = 3
#: How close (0..1) a path segment must be to an issuer-name word to be replaced (D191).
NAME_MATCH_MIN_RATIO = 0.75
#: The top levels a repaired portal URL may end in (D191).
ACCEPTED_TLDS = frozenset({"pe", "com", "net", "org"})
#: The most candidate URLs BR-F14 tries for one strip (D192).
MAX_CANDIDATES = 8
#: One look-alike pair a candidate host may differ by, in the order they are tried (D192).
LOOKALIKES: tuple[tuple[str, str], ...] = (
    ("0", "o"),
    ("1", "l"),
    ("5", "s"),
    ("8", "b"),
    ("vv", "w"),
    ("rn", "m"),
    ("l", "1"),
    ("l", "i"),
    ("i", "l"),
    ("o", "0"),
    ("m", "rn"),
)

#: A leading garbled or real scheme, dropped before the host is read (D191, D192).
_SCHEME_PATTERN = re.compile(
    r"^(?:h[^\s./]{2,5}(?:[\s:;.,]+[/\\|]+|[/\\|]{2,})|https?[\s:;.,]+)\s*",
    re.IGNORECASE,
)
#: A leading 4- to 6-character scheme look-alike the first pattern left in place (D192).
_GARBLED_SCHEME_PATTERN = re.compile(r"^([a-z0-9]{4,6})[.:]*[\s:/]+", re.IGNORECASE)
#: The last host labels OCR mangles for ``com``, whole labels only (D192).
_LAST_LABEL_REPAIRS: dict[str, tuple[str, ...]] = {
    "corn": ("com",),
    "compe": ("com", "pe"),
    "cornpe": ("com", "pe"),
}
#: The first host labels OCR mangles for ``www``, whole labels only (D192).
_FIRST_LABEL_REPAIRS = frozenset({"ww", "wwv", "vvww", "wvw", "wwvv"})
#: The separators OCR leaves where a dot was printed, repaired to a dot (D191).
_HOST_SEPARATOR_PATTERN = re.compile(r"[\s,;]+")
#: The characters a host may keep (D191).
_HOST_CHARS_PATTERN = re.compile(r"[^a-z0-9.-]")
#: The repeated dots a repaired host collapses (D191).
_REPEATED_DOTS_PATTERN = re.compile(r"\.{2,}")
#: The characters a path segment may keep, capitals included for the candidates (D191, D192).
_PATH_SEGMENT_CHARS_PATTERN = re.compile(r"[^a-z0-9._~-]", re.IGNORECASE)
#: One issuer-name word: a run of letters and digits (D191).
_NAME_WORD_PATTERN = re.compile(r"[a-z0-9]+")


def candidate_urls(text: str, issuer_name: str | None, known_hosts: frozenset[str]) -> tuple[str, ...]:
    """Return at most :data:`MAX_CANDIDATES` portal URLs the strip may mean, best first (BR-F14, D192).

    A host read as SUNAT's gives no candidate, so no look-alike of it is ever offered (R-5.2).
    """
    host_part, path_part = _host_and_path(text)
    host = _repair_host_read(host_part)
    if is_sunat_host(host):
        return ()
    paths = _candidate_paths(path_part, issuer_name)
    return _accepted_urls(_candidate_pairs(host_part, host, paths, known_hosts))


def _host_and_path(text: str) -> tuple[str, str]:
    """Return the strip's lowercased host part and its path part, capitals kept (D192)."""
    host_part, _, path_part = _strip_scheme(text.strip()).partition("/")
    return host_part.strip().lower(), path_part


def _candidate_paths(path_part: str, issuer_name: str | None) -> tuple[tuple[str, ...], ...]:
    """Return the paths to try, the issuer-repaired one first and without repeats (D192)."""
    segments = tuple(_read_segments(path_part))
    if not segments:
        return ((),)
    issuer_path = _issuer_path(segments, issuer_name)
    lowered = tuple(segment.lower() for segment in segments)
    paths = (issuer_path, segments, lowered) if issuer_path is not None else (segments, lowered)
    return tuple(dict.fromkeys(paths))


def _issuer_path(segments: tuple[str, ...], issuer_name: str | None) -> tuple[str, ...] | None:
    """Return the lowercase path with its last segment repaired from the issuer name, else None (D192)."""
    if issuer_name is None:
        return None
    words = _issuer_words(issuer_name)
    last = segments[-1].lower()
    repaired = _repair_segment(last, words)
    if repaired == last and last not in words:
        return None
    return (*tuple(segment.lower() for segment in segments[:-1]), repaired)


def _candidate_pairs(
    host_part: str,
    host: str,
    paths: tuple[tuple[str, ...], ...],
    known_hosts: frozenset[str],
) -> Iterator[tuple[str, tuple[str, ...]]]:
    """Yield the host and path pairs to try, one rule of D192 at a time, in its order."""
    first_path = paths[0]
    for path in paths:
        yield host, path
    known_host = _closest_known_host(host, known_hosts)
    if known_host != host:
        yield known_host, first_path
    spaced_host = _spaced_host(host_part)
    if spaced_host is not None:
        yield spaced_host, first_path
    yield _toggle_www(host), first_path
    for lookalike_host in _lookalike_hosts(host):
        yield lookalike_host, first_path


def _spaced_host(host_part: str) -> str | None:
    """Return the host with its inner spaces removed, or None when it has none (D192)."""
    if " " not in host_part:
        return None
    return _repair_host_read(host_part.replace(" ", ""))


def _toggle_www(host: str) -> str:
    """Return the host with a leading ``www.`` added, or removed when it has one (D192)."""
    if host.startswith("www."):
        return host.removeprefix("www.")
    return f"www.{host}"


def _lookalike_hosts(host: str) -> Iterator[str]:
    """Yield the host with one look-alike pair replaced at one place before the last label (D192)."""
    if "." not in host:
        return
    head, _, last_label = host.rpartition(".")
    head = f"{head}."
    for old, new in LOOKALIKES:
        start = 0
        while (index := head.find(old, start)) != -1:
            yield f"{head[:index]}{new}{head[index + len(old) :]}{last_label}"
            start = index + 1


def _accepted_urls(pairs: Iterator[tuple[str, tuple[str, ...]]]) -> tuple[str, ...]:
    """Return the accepted URLs among ``pairs``, without repeats and at most MAX_CANDIDATES (D192)."""
    urls: dict[str, None] = {}
    for host, path in pairs:
        url = _portal_url(host, path)
        if url is not None:
            urls.setdefault(url, None)
        if len(urls) >= MAX_CANDIDATES:
            break
    return tuple(urls)


def _portal_url(host: str, segments: tuple[str, ...]) -> str | None:
    """Return the URL of ``host`` with ``segments`` when :func:`_is_portal_url` accepts it (D192)."""
    suffix = f"/{'/'.join(segments)}" if segments else ""
    url = f"https://{host}{suffix}"
    return url if _is_portal_url(host, url) else None


def _strip_scheme(strip: str) -> str:
    """Return the strip without a leading real or garbled scheme (D191, D192)."""
    stripped = _SCHEME_PATTERN.sub("", strip)
    if stripped != strip:
        return stripped
    return _GARBLED_SCHEME_PATTERN.sub(_drop_garbled_scheme, strip)


def _drop_garbled_scheme(match: re.Match[str]) -> str:
    """Return nothing when the matched token is close to a scheme, else the match untouched (D192)."""
    token = match.group(1).lower()
    if any(
        SequenceMatcher(None, token, scheme).ratio() >= SCHEME_MATCH_MIN_RATIO for scheme in ("https", "http")
    ):
        return ""
    return match.group(0)


def _repair_host_read(host_part: str) -> str:
    """Return the host with OCR separators and labels repaired, before any known host (D191, D192)."""
    host = _HOST_SEPARATOR_PATTERN.sub(".", host_part)
    host = _REPEATED_DOTS_PATTERN.sub(".", _HOST_CHARS_PATTERN.sub("", host)).strip(".")
    return _repair_host_labels(host)


def _repair_host_labels(host: str) -> str:
    """Return the host with its whole first and last labels repaired where OCR mangled them (D192)."""
    labels = host.split(".")
    if len(labels) >= 2 and labels[-2:] == ["corn", "pe"]:
        labels[-2] = "com"
    elif labels[-1] in _LAST_LABEL_REPAIRS:
        labels[-1:] = _LAST_LABEL_REPAIRS[labels[-1]]
    if labels[0] in _FIRST_LABEL_REPAIRS:
        labels[0] = "www"
    return ".".join(labels)


def _closest_known_host(host: str, known_hosts: frozenset[str]) -> str:
    """Return the known host closest to ``host`` when close enough, else ``host`` (D191)."""
    best: tuple[float, str] | None = None
    for known in sorted(known_hosts):
        ratio = SequenceMatcher(None, host, known).ratio()
        if ratio >= HOST_MATCH_MIN_RATIO and (best is None or ratio > best[0]):
            best = (ratio, known)
    return host if best is None else best[1]


def _read_segments(path_part: str) -> list[str]:
    """Return the path's segments with unsafe characters dropped, last trailing dots gone (D191, D192)."""
    segments: list[str] = []
    for segment in path_part.split("/"):
        cleaned = _PATH_SEGMENT_CHARS_PATTERN.sub("", segment)
        if cleaned:
            segments.append(cleaned)
    return _drop_trailing_dots(segments)


def _drop_trailing_dots(segments: list[str]) -> list[str]:
    """Return the segments with the last one's trailing periods removed, dropping it when empty (D192)."""
    if not segments:
        return segments
    last = segments[-1].rstrip(".")
    if not last:
        return segments[:-1]
    return [*segments[:-1], last]


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
