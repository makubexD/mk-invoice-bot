"""Decides which read RUC is the issuer and which the buyer (R-2.4, D199)."""

from __future__ import annotations

from dataclasses import replace

from invoicebot.extract.fields import Reading


def with_own_buyer(reading: Reading, own_rucs: frozenset[str]) -> Reading:
    """Swap issuer and buyer when the read issuer is ours and the read buyer is not (D199).

    A reader that takes our own RUC for the issuer has read the buyer's block as the
    issuer's; each field keeps its source and confidence under the other name. Any other
    reading is returned as it is.
    """
    issuer = reading.value("ruc_emisor")
    buyer = reading.value("num_doc_adq")
    if issuer not in own_rucs or buyer is None or buyer in own_rucs:
        return reading
    fields = {
        **reading.fields,
        "ruc_emisor": reading.fields["num_doc_adq"],
        "num_doc_adq": reading.fields["ruc_emisor"],
    }
    return replace(reading, fields=fields)
