#!/usr/bin/env python3
"""Print one UBL invoice XML as its D127 expected file (D127, R-2.7, R-2.12).

Usage: ``expected_from_xml.py <xml> [--no-qr]``. The hardened ``parse_invoice_xml`` reads the
invoice and only the D127 fields survive, so ``tipo_doc_adq`` and every other extra field are
dropped before ``invoicebot.extract.accuracy.load_expected`` sees the JSON. ``--no-qr`` records
that the receipt's photo carries no SUNAT QR, which takes it out of the QR rate (R-2.9).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from invoicebot.extract.accuracy import EXPECTED_FIELDS
from invoicebot.extract.ubl import XmlRejected, parse_invoice_xml


def _iso_date(value: str) -> str:
    """Return ``value`` as an ISO date when it is one, else unchanged."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return value


def expected_from_xml(data: bytes, has_qr: bool = True) -> dict[str, object]:
    """Return the D127 expected object for the invoice XML in ``data`` (D127)."""
    reading = parse_invoice_xml(data)
    fields: dict[str, str] = {}
    for name in sorted(EXPECTED_FIELDS):
        value = reading.value(name)
        if value is None:
            continue
        fields[name] = _iso_date(value) if name == "fecha_emision" else value
    return {"has_qr": has_qr, "fields": fields}


def _parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the expected-file helper."""
    parser = argparse.ArgumentParser(description="Print a UBL invoice XML as its D127 expected JSON.")
    parser.add_argument("xml", type=Path, help="the invoice XML file")
    parser.add_argument("--no-qr", action="store_true", help="record that the photo carries no SUNAT QR")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print the expected JSON and return 0, or 1 when the XML cannot be read (R-2.12)."""
    args = _parser().parse_args(argv)
    try:
        expected = expected_from_xml(args.xml.read_bytes(), has_qr=not args.no_qr)
    except (OSError, XmlRejected) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(expected, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
