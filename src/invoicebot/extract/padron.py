"""Fill an issuer's razón social from the padrón (R-2.5).

The padrón is the source of an issuer's legal name and the OCR text is only the fallback,
so the pipeline hands the name it looked up to :func:`with_padron`. The module imports no
store code: the caller reads the padrón and passes the name in.
"""

from __future__ import annotations

from dataclasses import replace

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading


def with_padron(reading: Reading, razon_social: str | None) -> Reading:
    """Return ``reading`` with ``razon_social`` taken from the padrón when given (R-2.5).

    A ``None`` name means the padrón has no row for the issuer, so the reading keeps
    whatever its reader found (R-2.5).
    """
    if razon_social is None:
        return reading
    return replace(
        reading,
        fields={**reading.fields, "razon_social": Field(razon_social, "padron", FULL_CONFIDENCE)},
    )
