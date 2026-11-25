"""Read invoice fields from OCR text with keyword-anchored rules (S3.6, R-2.3, R-2.10, R-2.14).

The rules live in ``config/parsing_rules.yaml`` and are loaded and compiled once by
:mod:`invoicebot.extract.parsing_rules`. Each field is read from the first matching rule, its
confidence is the mean confidence of the OCR words its match covers, and ``source`` is ``"ocr"``.
The OCR text is never logged (R-9.16).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from dataclasses import dataclass

from invoicebot.extract.fields import (
    MAX_PRINTED_URL_CHARS,
    PRINTED_URL_PATH_CHARS,
    Field,
    Flag,
    Reading,
    is_printed_host,
)
from invoicebot.extract.ocr import OcrText
from invoicebot.extract.parsing_rules import ParsingRules
from invoicebot.extract.qr_payload import (
    DOCUMENT_TYPE_BY_SERIE_LETTER,
    canonical_numero,
    parse_amount,
    parse_date,
)
from invoicebot.extract.validate import check_serie, is_valid_ruc
from invoicebot.sunat_origins import is_sunat_host

WORD_SEPARATOR = " "
LINE_SEPARATOR = "\n"
RUC_DOCUMENT_TYPE = "6"
SERIE_FIX_LENGTH = 3
SERIE_DIGIT_FIXES = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1"}
AMOUNT_SEPARATOR = "."
AMOUNT_SEPARATORS = frozenset(".,")
AMOUNT_DECIMALS = 2
AMOUNT_SEPARATOR_OFFSET = AMOUNT_DECIMALS + 1
AMOUNT_SEPARATORS_PATTERN = re.compile(r"[.,]")
NO_CONFIDENCE = 0.0
#: Most OCR text characters parsed, so a huge read cannot exhaust the helper (R-9.2).
MAX_OCR_TEXT_CHARS = 64 * 1024
#: Most matches considered per parsing rule, so a pathological text cannot exhaust it (R-9.2).
MAX_MATCHES_PER_RULE = 50
#: The scheme every printed URL is normalized to (D189).
HTTPS_SCHEME = "https://"
#: Punctuation a sentence puts after a printed URL, never part of it (D189).
TRAILING_URL_PUNCTUATION = ".,;:)"
#: A printed address: an optional scheme, a dotted host and an optional path (D189).
#: One quantifier per part and a boundary that refuses a start inside a longer word, so the
#: scan stays linear on untrusted OCR text (R-9.2).
PRINTED_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9.-])"
    r"(?P<scheme>https?://)?"
    r"(?P<host>[A-Za-z0-9.-]+)"
    rf"(?P<path>/{PRINTED_URL_PATH_CHARS}*)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OcrReading:
    """What the OCR reader produced: the reading."""

    reading: Reading


@dataclass(frozen=True)
class _WordSpan:
    """The character span of one OCR word and its confidence."""

    start: int
    end: int
    confidence: float


def parse_ocr(text: OcrText, rules: ParsingRules) -> OcrReading:
    """Return the fields read from ``text`` with ``rules`` (S3.6, R-2.3)."""
    searched, spans = _build_text(text)
    searched = searched[:MAX_OCR_TEXT_CHARS]
    fields: dict[str, Field] = {}
    _add_rucs(fields, searched, spans, rules)
    serie, numero = _read_serie_numero(searched, spans, rules)
    if serie is not None:
        fields["serie"] = serie
    if numero is not None:
        fields["numero"] = numero
    tipo = _read_tipo(searched, spans, rules, serie)
    if tipo is not None:
        fields["tipo"] = tipo
    fecha = _read_date(searched, spans, rules)
    if fecha is not None:
        fields["fecha_emision"] = fecha
    fields.update(_read_amounts(searched, spans, rules))
    reading = Reading(fields=fields, document_url=None, printed_url=_read_printed_url(searched))
    return OcrReading(reading=reading)


def _read_printed_url(searched: str) -> str | None:
    """Return the first web address printed in ``searched``, normalized to https, or None (D189)."""
    for match in PRINTED_URL_PATTERN.finditer(searched):
        path = match.group("path") or ""
        if not match.group("scheme") and not path:
            continue
        if match.start("host") > 0 and searched[match.start("host") - 1] == "@":
            continue
        host = match.group("host").lower().rstrip(".")
        if not is_printed_host(host):
            continue
        if is_sunat_host(host):
            continue
        url = f"{HTTPS_SCHEME}{host}{path.rstrip(TRAILING_URL_PUNCTUATION)}"
        if len(url) > MAX_PRINTED_URL_CHARS:
            return None
        return url
    return None


def _matches(pattern: re.Pattern[str], searched: str) -> Iterator[re.Match[str]]:
    """Return at most :data:`MAX_MATCHES_PER_RULE` matches of ``pattern`` in ``searched``."""
    return itertools.islice(pattern.finditer(searched), MAX_MATCHES_PER_RULE)


def _build_text(text: OcrText) -> tuple[str, tuple[_WordSpan, ...]]:
    """Join the OCR words and lines, recording each word's span in the joined text."""
    lines: list[str] = []
    spans: list[_WordSpan] = []
    offset = 0
    for line in text.lines:
        line_text = WORD_SEPARATOR.join(word.text for word in line)
        position = offset
        for word in line:
            end = position + len(word.text)
            spans.append(_WordSpan(start=position, end=end, confidence=word.confidence))
            position = end + len(WORD_SEPARATOR)
        lines.append(line_text)
        offset += len(line_text) + len(LINE_SEPARATOR)
    return LINE_SEPARATOR.join(lines), tuple(spans)


def _ocr_field(value: str, confidence: float, flag: Flag | None = None) -> Field:
    """Return an OCR-sourced field with ``confidence`` and ``flag``."""
    return Field(value=value, source="ocr", confidence=confidence, flag=flag)


def _confidence(spans: tuple[_WordSpan, ...], start: int, end: int) -> float:
    """Return the mean confidence of the words overlapping ``start``..``end``."""
    overlapping = [span.confidence for span in spans if span.start < end and span.end > start]
    return sum(overlapping) / len(overlapping) if overlapping else NO_CONFIDENCE


def _add_rucs(
    fields: dict[str, Field],
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
) -> None:
    """Add the issuer RUC and, when a second valid RUC is printed, the buyer (R-2.4)."""
    found = _ruc_matches(searched, spans, rules)
    if not found:
        return
    fields["ruc_emisor"] = _ocr_field(*found[0])
    if len(found) > 1:
        value, confidence = found[1]
        fields["num_doc_adq"] = _ocr_field(value, confidence)
        fields["tipo_doc_adq"] = _ocr_field(RUC_DOCUMENT_TYPE, confidence, flag="inferred")


def _ruc_matches(
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
) -> list[tuple[str, float]]:
    """Return the distinct valid RUCs, keyword-anchored matches first, each in text order."""
    found: list[tuple[str, float]] = []
    seen: set[str] = set()
    for pattern in rules.ruc:
        for match in _matches(pattern, searched):
            value = match.group("value")
            if value in seen or not is_valid_ruc(value):
                continue
            seen.add(value)
            found.append((value, _confidence(spans, *match.span("value"))))
    return found


def _read_serie_numero(
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
) -> tuple[Field | None, Field | None]:
    """Return the first letter serie, else the first four-digit serie (R-2.3).

    A candidate whose repaired serie fails :func:`check_serie` is skipped, numero included.
    A letter serie outranks a four-digit one, which is usually an address or a date. A letter
    serie needs a digit after its letter, so a word such as ``BASE`` is not one.
    """
    fallback: tuple[re.Match[str], str] | None = None
    for match in _matches(rules.serie_numero, searched):
        serie = _fix_serie(match.group("serie").upper())
        if not check_serie(serie):
            continue
        if serie[:1].isalpha() and any(char.isdigit() for char in serie[1:]):
            return _serie_numero_fields(match, serie, spans)
        if serie.isdigit() and fallback is None:
            fallback = (match, serie)
    if fallback is None:
        return None, None
    match, serie = fallback
    return _serie_numero_fields(match, serie, spans)


def _serie_numero_fields(
    match: re.Match[str],
    serie: str,
    spans: tuple[_WordSpan, ...],
) -> tuple[Field, Field]:
    """Return the serie and numero fields for one accepted serie-numero match."""
    numero = canonical_numero(match.group("numero"))
    return (
        _ocr_field(serie, _confidence(spans, *match.span("serie"))),
        _ocr_field(numero, _confidence(spans, *match.span("numero"))),
    )


def _fix_serie(serie: str) -> str:
    """Map the serie's last three OCR letters back to digits (S0.3)."""
    fixed = "".join(SERIE_DIGIT_FIXES.get(char, char) for char in serie[-SERIE_FIX_LENGTH:])
    return serie[:-SERIE_FIX_LENGTH] + fixed


def _read_tipo(
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
    serie: Field | None,
) -> Field | None:
    """Return the first document-type keyword found, else infer it from the serie (R-2.3)."""
    best: tuple[int, str, float] | None = None
    for code, patterns in rules.tipo.items():
        for pattern in patterns:
            match = pattern.search(searched)
            if match is not None and (best is None or match.start() < best[0]):
                best = (match.start(), code, _confidence(spans, *match.span()))
    if best is not None:
        return _ocr_field(best[1], best[2])
    if serie is None:
        return None
    inferred = DOCUMENT_TYPE_BY_SERIE_LETTER.get(serie.value[:1])
    return None if inferred is None else _ocr_field(inferred, serie.confidence, flag="inferred")


def _read_date(
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
) -> Field | None:
    """Return the first date the shared QR date parser accepts, as an ISO string (R-2.3)."""
    for match in _matches(rules.date, searched):
        value = match.group("value")
        normalized = value.replace("-", "/") if value[2] == "-" else value
        iso = parse_date(normalized)
        if iso is not None:
            return _ocr_field(iso, _confidence(spans, *match.span("value")))
    return None


def _read_amounts(
    searched: str,
    spans: tuple[_WordSpan, ...],
    rules: ParsingRules,
) -> dict[str, Field]:
    """Return every printed amount field, each from its first parseable keyword match."""
    fields: dict[str, Field] = {}
    for name, patterns in rules.amount_patterns.items():
        amount = _read_amount(searched, spans, patterns)
        if amount is not None:
            fields[name] = amount
    return fields


def _read_amount(
    searched: str,
    spans: tuple[_WordSpan, ...],
    patterns: tuple[re.Pattern[str], ...],
) -> Field | None:
    """Return the first amount a keyword-gap-value pattern reads and the amount parser accepts."""
    for pattern in patterns:
        for match in _matches(pattern, searched):
            amount = parse_amount(_normalise_amount(match.group("value")))
            if amount is not None:
                return _ocr_field(str(amount), _confidence(spans, *match.span("value")))
    return None


def _normalise_amount(text: str) -> str:
    """Drop whitespace and make the last separator the decimal point, so ``parse_amount`` reads it."""
    compact = "".join(text.split())
    if len(compact) > AMOUNT_DECIMALS and compact[-AMOUNT_SEPARATOR_OFFSET] in AMOUNT_SEPARATORS:
        whole = AMOUNT_SEPARATORS_PATTERN.sub("", compact[:-AMOUNT_SEPARATOR_OFFSET])
        return f"{whole}{AMOUNT_SEPARATOR}{compact[-AMOUNT_DECIMALS:]}"
    return compact
