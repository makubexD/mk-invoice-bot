"""The parsing rules read from ``config/parsing_rules.yaml``, compiled once at load (S3.6).

Every pattern is compiled and its named groups checked while the file loads, so a broken rule stops
the run instead of failing on the first invoice. This module stays free of OpenCV and Tesseract so
the app can load the rules in its own process (D78).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, BeforeValidator, ConfigDict, PrivateAttr, model_validator

from invoicebot.extract.fields import AMOUNT_FIELDS
from invoicebot.extract.qr_payload import DOCUMENT_TYPE_BY_SERIE_LETTER

#: The document type codes the OCR reader can map a ``tipo`` rule to.
DOCUMENT_TYPE_CODES = frozenset(DOCUMENT_TYPE_BY_SERIE_LETTER.values())


def _compile_rule(value: object, required_groups: tuple[str, ...]) -> re.Pattern[str]:
    """Compile one rule pattern, raising ValueError when it breaks or lacks a named group."""
    if not isinstance(value, str):
        raise ValueError("a parsing rule pattern must be a string")
    try:
        compiled = re.compile(value, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"parsing rule pattern {value!r} does not compile: {exc}") from exc
    missing = [group for group in required_groups if group not in compiled.groupindex]
    if missing:
        raise ValueError(f"parsing rule pattern {value!r} lacks named group(s) {', '.join(missing)}")
    return compiled


def _compile_value(value: object) -> re.Pattern[str]:
    """Compile a pattern whose ``value`` group carries the extracted text."""
    return _compile_rule(value, ("value",))


def _compile_keyword(value: object) -> re.Pattern[str]:
    """Compile a keyword pattern, which captures nothing."""
    return _compile_rule(value, ())


def _compile_serie_numero(value: object) -> re.Pattern[str]:
    """Compile the serie-and-number pattern, which needs both its named groups."""
    return _compile_rule(value, ("serie", "numero"))


ValuePattern = Annotated[re.Pattern[str], BeforeValidator(_compile_value)]
KeywordPattern = Annotated[re.Pattern[str], BeforeValidator(_compile_keyword)]
SerieNumeroPattern = Annotated[re.Pattern[str], BeforeValidator(_compile_serie_numero)]


class ParsingRules(BaseModel):
    """The validated parsing rules, with every pattern compiled at load time (S3.6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruc: tuple[ValuePattern, ...]
    tipo: dict[str, tuple[KeywordPattern, ...]]
    serie_numero: SerieNumeroPattern
    date: ValuePattern
    amount_gap: KeywordPattern
    amount_value: ValuePattern
    amounts: dict[str, tuple[KeywordPattern, ...]]
    printed_url: ValuePattern

    _amount_patterns: dict[str, tuple[re.Pattern[str], ...]] = PrivateAttr()

    @model_validator(mode="after")
    def _check_field_names(self) -> Self:
        """Reject an amount or document type the readers do not produce (S3.6)."""
        unknown_amounts = sorted(name for name in self.amounts if name not in AMOUNT_FIELDS)
        if unknown_amounts:
            raise ValueError(f"unknown amount field(s) {', '.join(unknown_amounts)}")
        unknown_types = sorted(code for code in self.tipo if code not in DOCUMENT_TYPE_CODES)
        if unknown_types:
            raise ValueError(f"unknown document type code(s) {', '.join(unknown_types)}")
        return self

    @model_validator(mode="after")
    def _build_amount_patterns(self) -> Self:
        """Join each amount keyword with the gap and value rules, once at load time."""
        combined = {
            name: tuple(
                _compile_rule(
                    f"(?:{keyword.pattern}){self.amount_gap.pattern}(?:{self.amount_value.pattern})", ()
                )
                for keyword in keywords
            )
            for name, keywords in self.amounts.items()
        }
        object.__setattr__(self, "_amount_patterns", combined)
        return self

    @property
    def amount_patterns(self) -> dict[str, tuple[re.Pattern[str], ...]]:
        """Return the compiled keyword-gap-value patterns per amount field, in file order."""
        return self._amount_patterns


def load_rules(path: Path) -> ParsingRules:
    """Read and validate the parsing-rule file; raise ValueError when it is not usable (S3.6)."""
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError(f"parsing rules root must be a mapping, got {type(document).__name__}")
    return ParsingRules.model_validate(document)
