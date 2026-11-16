"""Read the go-live import's claim folders: one XML and its folder name into a claim (R-7.16).

One job: turn a reimbursement folder's name and its single XML into the claim the import
registers, or into the reason it is skipped. It reads only the folder it is given and
writes nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from invoicebot.extract.ubl import MAX_XML_BYTES, XmlRejected, parse_invoice_xml

#: ``{YYYY}-{M or MM}-Support_{seq}_{comercio}`` with the optional ``_Sent`` and
#: ``_PAID_{dd}_{mm}___{amount}`` suffixes the sink appends (R-7.11, R-7.16).
FOLDER_NAME_PATTERN = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{1,2})-Support_(?P<seq>\d+)_"
    r"(?P<comercio>[^/\\]+?)"
    r"(?P<sent>_Sent)?"
    r"(?P<paid>_PAID_\d{2}_\d{2}___\d+(?:[._]\d+)?)?$"
)
MIN_MONTH = 1
MAX_MONTH = 12
MIN_SEQ = 1

SKIP_NAME = "not a claim folder name"
SKIP_PAID = "already paid"
SKIP_UNSENT = "not sent"
SKIP_NO_XML = "no XML in the folder"
SKIP_SEVERAL_XML = "more than one XML in the folder"
SKIP_XML_TOO_LARGE = "the XML is over the size cap"
SKIP_XML_UNREADABLE = "the XML cannot be read"
SKIP_XML_UNPARSEABLE = "the XML is not a readable invoice"
SKIP_FIELDS_MISSING = "the XML lacks a required claim field"


@dataclass(frozen=True)
class ClaimFolder:
    """The claim data a folder name carries: its month, seq, comercio and status (R-7.16)."""

    year: int
    month: int
    seq: int
    comercio: str
    sent: bool
    paid: bool


@dataclass(frozen=True)
class Claim:
    """One sent, unpaid claim read from a folder, with the folder it came from (R-7.16)."""

    ruc: str
    tipo: str
    serie: str
    numero: str
    fecha: date
    total: Decimal
    razon_social: str
    folder: ClaimFolder


@dataclass(frozen=True)
class Skipped:
    """A folder the import does not register, and the reason the script prints (R-7.16)."""

    reason: str


def parse_claim_folder(name: str) -> ClaimFolder | None:
    """Return the claim data in a folder name, or ``None`` when it is not one (R-7.16).

    The month may be written with or without its leading zero, and the comercio may not
    hold a path separator.
    """
    match = FOLDER_NAME_PATTERN.match(name)
    if match is None:
        return None
    month = int(match.group("month"))
    seq = int(match.group("seq"))
    if not MIN_MONTH <= month <= MAX_MONTH or seq < MIN_SEQ:
        return None
    return ClaimFolder(
        year=int(match.group("year")),
        month=month,
        seq=seq,
        comercio=match.group("comercio"),
        sent=match.group("sent") is not None,
        paid=match.group("paid") is not None,
    )


def read_claim(folder: Path) -> Claim | Skipped:
    """Return the claim ``folder`` holds, or the reason it is not one (R-7.16).

    Only a sent and unpaid folder with exactly one readable XML under the size cap counts
    (D110).
    """
    claim_folder = parse_claim_folder(folder.name)
    if claim_folder is None:
        return Skipped(SKIP_NAME)
    if claim_folder.paid:
        return Skipped(SKIP_PAID)
    if not claim_folder.sent:
        return Skipped(SKIP_UNSENT)
    xmls = sorted(folder.glob("*.xml"))
    if not xmls:
        return Skipped(SKIP_NO_XML)
    if len(xmls) > 1:
        return Skipped(SKIP_SEVERAL_XML)
    return _claim_from_xml(xmls[0], claim_folder)


def _claim_from_xml(xml: Path, folder: ClaimFolder) -> Claim | Skipped:
    """Return the claim in ``xml``, or the reason it cannot be read (R-7.16)."""
    try:
        if xml.stat().st_size > MAX_XML_BYTES:
            return Skipped(SKIP_XML_TOO_LARGE)
        data = xml.read_bytes()
    except OSError:
        return Skipped(SKIP_XML_UNREADABLE)
    try:
        reading = parse_invoice_xml(data)
    except XmlRejected:
        return Skipped(SKIP_XML_UNPARSEABLE)
    ruc = reading.value("ruc_emisor")
    tipo = reading.value("tipo")
    serie = reading.value("serie")
    numero = reading.value("numero")
    fecha = reading.fecha()
    total = reading.amount("total")
    razon_social = reading.value("razon_social")
    if (
        ruc is None
        or tipo is None
        or serie is None
        or numero is None
        or fecha is None
        or total is None
        or razon_social is None
    ):
        return Skipped(SKIP_FIELDS_MISSING)
    return Claim(
        ruc=ruc,
        tipo=tipo,
        serie=serie,
        numero=numero,
        fecha=fecha,
        total=total,
        razon_social=razon_social,
        folder=folder,
    )
