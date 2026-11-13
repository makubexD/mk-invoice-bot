"""Hardened UBL 2.1 reading of an invoice XML (R-2.7, R-2.12, R-5.6).

Only the fields the R-5.6 match needs and the amounts of R-2.7 are read (D57). Every XML,
whatever its source, goes through the same parser: no entity resolution, no DTD loading and
no network access, and a ``DOCTYPE`` or a document over 2 MB is rejected before lxml sees
it. Elements are found by local name, so the namespace prefix does not matter.
"""

from __future__ import annotations

from enum import StrEnum

from lxml import etree  # parser hardened in parse_invoice_xml (R-2.12)

from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading, finite_decimal

MAX_XML_BYTES = 2 * 1024 * 1024
DOCTYPE_MARKER = b"<!doctype"
INVOICE_ROOT = "Invoice"
AMOUNT_ELEMENTS = (
    ("base", "LineExtensionAmount"),
    ("service_charge", "ChargeTotalAmount"),
    ("discounts", "AllowanceTotalAmount"),
    ("total", "PayableAmount"),
)


class XmlReason(StrEnum):
    """Why an XML was rejected; every reason maps to BR-F09 at the caller (R-2.12)."""

    TOO_LARGE = "too_large"
    DOCTYPE = "doctype"
    MALFORMED = "malformed"
    NOT_INVOICE = "not_invoice"


class XmlRejected(Exception):
    """An XML that cannot be read safely or is not an invoice (BR-F09, R-2.12)."""

    def __init__(self, reason: XmlReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def parse_invoice_xml(data: bytes) -> Reading:
    """Read the match fields and amounts from a UBL invoice XML (R-2.7, R-2.12).

    Raises:
        XmlRejected: with ``too_large``, ``doctype``, ``malformed`` or ``not_invoice``.
    """
    if len(data) > MAX_XML_BYTES:
        raise XmlRejected(XmlReason.TOO_LARGE)
    if DOCTYPE_MARKER in data.lower():
        raise XmlRejected(XmlReason.DOCTYPE)

    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(data, parser)
    except etree.XMLSyntaxError as exc:
        raise XmlRejected(XmlReason.MALFORMED) from exc

    docinfo = root.getroottree().docinfo
    if docinfo.doctype or docinfo.internalDTD is not None:
        raise XmlRejected(XmlReason.DOCTYPE)
    if root.xpath("local-name(.)") != INVOICE_ROOT:
        raise XmlRejected(XmlReason.NOT_INVOICE)
    return Reading(fields=_read_fields(root), document_url=None)


def matches_invoice(xml: Reading, invoice: Reading) -> bool:
    """Return whether ``xml`` is the same invoice as ``invoice`` (R-5.6).

    Compares the issuer RUC, the serie, the numero as a number and the total. A reading
    missing any of those fields does not match.
    """
    xml_ruc = xml.value("ruc_emisor")
    if xml_ruc is None or xml_ruc != invoice.value("ruc_emisor"):
        return False
    xml_serie = xml.value("serie")
    if xml_serie is None or xml_serie != invoice.value("serie"):
        return False
    xml_number = _integer(xml.value("numero"))
    invoice_number = _integer(invoice.value("numero"))
    if xml_number is None or invoice_number is None or xml_number != invoice_number:
        return False
    xml_total = xml.amount("total")
    invoice_total = invoice.amount("total")
    return xml_total is not None and xml_total == invoice_total


def _read_fields(root: etree._Element) -> dict[str, Field]:
    """Return the fields this reader takes, all XML-sourced at full confidence (R-2.6)."""
    fields: dict[str, Field] = {}
    _add_id(fields, _text(_child(root, "ID")))
    _add(fields, "fecha_emision", _text(_child(root, "IssueDate")))
    _add(fields, "tipo", _text(_child(root, "InvoiceTypeCode")))

    supplier = _child(root, "AccountingSupplierParty")
    _add(fields, "ruc_emisor", _text(_party_id(supplier)))
    _add(fields, "razon_social", _text(_party_legal_name(supplier)))

    customer_id = _party_id(_child(root, "AccountingCustomerParty"))
    _add(fields, "num_doc_adq", _text(customer_id))
    if customer_id is not None:
        _add(fields, "tipo_doc_adq", _attribute(customer_id, "schemeID"))

    _read_amounts(fields, _child(root, "LegalMonetaryTotal"))
    _add_amount(fields, "igv", _tax_amount(root))
    return fields


def _read_amounts(fields: dict[str, Field], monetary_total: etree._Element | None) -> None:
    """Store each amount found under ``LegalMonetaryTotal`` (R-2.7)."""
    for name, local_name in AMOUNT_ELEMENTS:
        _add_amount(fields, name, _text(_child(monetary_total, local_name)))


def _party_id(party: etree._Element | None) -> etree._Element | None:
    """Return ``Party/PartyIdentification/ID`` for a supplier or customer party."""
    return _path(party, "Party", "PartyIdentification", "ID")


def _party_legal_name(party: etree._Element | None) -> etree._Element | None:
    """Return ``Party/PartyLegalEntity/RegistrationName`` for a supplier or customer party."""
    return _path(party, "Party", "PartyLegalEntity", "RegistrationName")


def _tax_amount(root: etree._Element) -> str | None:
    """Return the document-level IGV amount (R-2.7).

    Only a ``TaxTotal`` that is a direct child of the invoice counts: an ``InvoiceLine``
    carries its own ``TaxTotal``, and a ``TaxSubtotal`` carries its own ``TaxAmount``.
    """
    for tax_total in root.xpath("./*[local-name() = 'TaxTotal']"):
        amount = _text(_child(tax_total, "TaxAmount"))
        if amount is not None:
            return amount
    return None


def _add_id(fields: dict[str, Field], value: str | None) -> None:
    """Split the document ID into ``serie`` and ``numero`` (``F014-15160``)."""
    if value is None:
        return
    serie, separator, numero = value.partition("-")
    if separator and serie and numero:
        _add(fields, "serie", serie)
        _add(fields, "numero", numero)


def _add(fields: dict[str, Field], name: str, value: str | None) -> None:
    """Store ``value`` when it is present; a missing optional value stays absent."""
    if value is not None:
        fields[name] = Field(value=value, source="xml", confidence=FULL_CONFIDENCE)


def _add_amount(fields: dict[str, Field], name: str, value: str | None) -> None:
    """Store a finite decimal amount, or nothing when it is absent or not a finite number."""
    amount = finite_decimal(value) if value is not None else None
    if amount is not None:
        fields[name] = Field(value=str(amount), source="xml", confidence=FULL_CONFIDENCE)


def _child(element: etree._Element | None, local_name: str) -> etree._Element | None:
    """Return the direct child of ``element`` with that local name, if any."""
    if element is None:
        return None
    matches = element.xpath(f"./*[local-name() = '{local_name}']")
    return matches[0] if matches else None


def _path(element: etree._Element | None, *names: str) -> etree._Element | None:
    """Return the element reached by walking direct children with these local names."""
    current = element
    for name in names:
        current = _child(current, name)
        if current is None:
            return None
    return current


def _text(element: etree._Element | None) -> str | None:
    """Return the stripped text of ``element``, or ``None`` when it is absent or empty."""
    if element is None or element.text is None:
        return None
    return element.text.strip() or None


def _attribute(element: etree._Element, name: str) -> str | None:
    """Return the stripped attribute ``name`` of ``element``, or ``None`` when absent or empty."""
    value = element.get(name)
    if value is None:
        return None
    return value.strip() or None


def _integer(value: str | None) -> int | None:
    """Return ``value`` as an integer, or ``None`` when it is absent or not a number."""
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None
