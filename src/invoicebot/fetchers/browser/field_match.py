"""Match Peruvian portal form fields to the invoice values they carry (D193).

Pure helpers for the portal probe. They turn a label or an HTML control name into
the invoice value a field holds, pick the ``tipo`` option of a select, and format an
invoice date to the format a placeholder asks for. No Playwright.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from datetime import date

from invoicebot.fetchers.browser.probe_protocol import ValueName

#: The invoice values, in the order a tie between them is broken by (D193).
_VALUE_ORDER: tuple[ValueName, ...] = ("tipo", "serie", "fecha", "total", "numero", "ruc_emisor")

#: Keywords that name each invoice value on a form (D193). One home per keyword list.
_KEYWORDS: dict[ValueName, tuple[str, ...]] = {
    "tipo": ("tipo",),
    "serie": ("serie",),
    "fecha": ("fecha", "emision"),
    "total": ("monto", "importe", "total"),
    "numero": ("correlativo", "numero", "nro", "folio"),
    "ruc_emisor": ("ruc", "emisor"),
}

#: Words that mean a field is not one of ours, wherever they appear (D193).
_EXCLUDED_WORDS: frozenset[str] = frozenset(
    {"captcha", "codigo", "usuario", "clave", "cliente", "receptor", "adquirente"}
)

#: Words that make a ``tipo`` option unusable (D193).
_TIPO_EXCLUDED_WORDS: frozenset[str] = frozenset({"nota", "exportacion"})


def fold(text: str) -> str:
    """Drop accents and case so words compare across typography (D193)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def words(text: str) -> frozenset[str]:
    """Fold *text* and split it into whole words (D193).

    Words are runs of letters or digits; ``N°`` becomes ``n`` so the degree sign
    does not split the surrounding words.
    """
    return frozenset(re.findall(r"[a-z0-9]+", fold(text)))


def _name_words(html_name: str) -> frozenset[str]:
    """Fold *html_name* into words at CamelCase, digits and separators (D193).

    ``ctl00$txtSerie`` becomes ``ctl``, ``txt``, ``serie``; ``txtRUCEmisor`` becomes
    ``txt``, ``ruc``, ``emisor``.
    """
    separated = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|"
        r"(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])",
        " ",
        html_name,
    )
    return words(separated)


def _match(words: frozenset[str]) -> ValueName | None:
    """Return the first value whose keyword is a whole word in *words* (D193)."""
    for value in _VALUE_ORDER:
        if any(keyword in words for keyword in _KEYWORDS[value]):
            return value
    return None


def value_name(label: str, html_name: str) -> ValueName | None:
    """Return the invoice value a form field carries, or None (D193).

    The label is folded and matched first; a label with an excluded word returns None
    without looking at the name. Otherwise the name is split into CamelCase, digit and
    separator parts and matched the same way. At most one value is returned, the first
    in the tie-break order.
    """
    label_words = words(label)
    if _EXCLUDED_WORDS & label_words:
        return None
    label_match = _match(label_words)
    if label_match is not None:
        return label_match
    name_words = _name_words(html_name)
    if _EXCLUDED_WORDS & name_words:
        return None
    return _match(name_words)


def tipo_option(options: Sequence[str], tipo: str) -> str | None:
    """Return the option of *options* that names *tipo* alone, or None (D193).

    ``01`` wants the word ``factura`` and ``03`` the word ``boleta``; any other *tipo*
    returns None. An option counts only when it has the wanted word, not the other one,
    and none of ``nota`` or ``exportacion``. The original text is returned when exactly
    one option counts.
    """
    wanted = {"01": "factura", "03": "boleta"}.get(tipo)
    if wanted is None:
        return None
    other = "boleta" if wanted == "factura" else "factura"
    counted: list[str] = []
    for option in options:
        option_words = words(option)
        usable = wanted in option_words and other not in option_words
        if usable and not (_TIPO_EXCLUDED_WORDS & option_words):
            counted.append(option)
    return counted[0] if len(counted) == 1 else None


def date_text(fecha: date, placeholder: str) -> str:
    """Format *fecha* as the stripped *placeholder* asks, any case (D193).

    An empty placeholder or ``yyyy-mm-dd`` gives ISO; ``dd-mm-yyyy`` gives dashes;
    anything else gives ``dd/mm/yyyy``. Spanish ``aaaa`` stands for ``yyyy``.
    """
    folded = fold(placeholder.strip()).replace("aaaa", "yyyy")
    if folded in ("", "yyyy-mm-dd"):
        return fecha.isoformat()
    if folded == "dd-mm-yyyy":
        return f"{fecha.day:02d}-{fecha.month:02d}-{fecha.year}"
    return f"{fecha.day:02d}/{fecha.month:02d}/{fecha.year}"
