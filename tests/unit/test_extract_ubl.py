"""Tests for the hardened UBL reading and the invoice match (R-2.7, R-2.12, R-5.6)."""

from __future__ import annotations

import pytest

from invoicebot.extract.fields import Field, Reading
from invoicebot.extract.ubl import (
    MAX_XML_BYTES,
    XmlReason,
    XmlRejected,
    matches_invoice,
    parse_invoice_xml,
)

# Both values come from tests/synthetic_rucs.txt.
SYNTHETIC_RUC = "20000000001"
SYNTHETIC_RUC_OTHER = "51900000000"
FULL_CONFIDENCE = 1.0

INVOICE_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
         xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
         xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>{serie}-{numero}</cbc:ID>
  <cbc:IssueDate>{fecha}</cbc:IssueDate>
  <cbc:InvoiceTypeCode>{tipo}</cbc:InvoiceTypeCode>
  <cac:AccountingSupplierParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="6">{ruc_emisor}</cbc:ID>
      </cac:PartyIdentification>
      <cac:PartyLegalEntity>
        <cbc:RegistrationName>{razon_social}</cbc:RegistrationName>
      </cac:PartyLegalEntity>
    </cac:Party>
  </cac:AccountingSupplierParty>
  <cac:AccountingCustomerParty>
    <cac:Party>
      <cac:PartyIdentification>
        <cbc:ID schemeID="{tipo_doc_adq}">{num_doc_adq}</cbc:ID>
      </cac:PartyIdentification>
    </cac:Party>
  </cac:AccountingCustomerParty>
  <cac:TaxTotal>
    <cbc:TaxAmount currencyID="PEN">{igv}</cbc:TaxAmount>
    <cac:TaxSubtotal>
      <cbc:TaxAmount currencyID="PEN">{subtotal_igv}</cbc:TaxAmount>
    </cac:TaxSubtotal>
  </cac:TaxTotal>
  <cac:LegalMonetaryTotal>
    <cbc:LineExtensionAmount currencyID="PEN">{base}</cbc:LineExtensionAmount>
    {allowance_element}
    {charge_element}
    <cbc:PayableAmount currencyID="PEN">{total}</cbc:PayableAmount>
  </cac:LegalMonetaryTotal>
  <cac:InvoiceLine>
    <cbc:LineExtensionAmount currencyID="PEN">{base}</cbc:LineExtensionAmount>
    <cac:TaxTotal>
      <cbc:TaxAmount currencyID="PEN">{line_igv}</cbc:TaxAmount>
    </cac:TaxTotal>
  </cac:InvoiceLine>
</Invoice>
"""

DEFAULT_VALUES: dict[str, str | None] = {
    "serie": "F014",
    "numero": "15160",
    "fecha": "2026-11-13",
    "tipo": "01",
    "ruc_emisor": SYNTHETIC_RUC,
    "razon_social": "RESTAURANTE EJEMPLO S.A.C.",
    "tipo_doc_adq": "6",
    "num_doc_adq": SYNTHETIC_RUC_OTHER,
    "base": "40.90",
    "igv": "7.36",
    "subtotal_igv": "9.99",
    "line_igv": "6.09",
    "total": "49.90",
    "service_charge": "1.64",
    "discounts": "0.00",
}

FULL_FIELDS = {
    "ruc_emisor": SYNTHETIC_RUC,
    "tipo": "01",
    "serie": "F014",
    "numero": "15160",
    "fecha_emision": "2026-11-13",
    "razon_social": "RESTAURANTE EJEMPLO S.A.C.",
    "tipo_doc_adq": "6",
    "num_doc_adq": SYNTHETIC_RUC_OTHER,
    "base": "40.90",
    "igv": "7.36",
    "service_charge": "1.64",
    "discounts": "0.00",
    "total": "49.90",
}


def _optional_element(tag: str, amount: str | None) -> str:
    """Return a currency amount element, or nothing when the amount is ``None``."""
    if amount is None:
        return ""
    return f'<cbc:{tag} currencyID="PEN">{amount}</cbc:{tag}>'


def _invoice_xml(**overrides: str | None) -> str:
    """Return the inline invoice XML, applying ``overrides`` to the default values."""
    values = {**DEFAULT_VALUES, **overrides}
    values["charge_element"] = _optional_element("ChargeTotalAmount", values.pop("service_charge"))
    values["allowance_element"] = _optional_element("AllowanceTotalAmount", values.pop("discounts"))
    return INVOICE_TEMPLATE.format_map(values)


def _reading(**overrides: str | None) -> Reading:
    """Return a QR reading with the default match fields, applying ``overrides``."""
    values = {
        "ruc_emisor": SYNTHETIC_RUC,
        "serie": "F014",
        "numero": "15160",
        "total": "49.90",
    }
    values.update(overrides)
    fields = {
        name: Field(value=value, source="qr", confidence=FULL_CONFIDENCE)
        for name, value in values.items()
        if value is not None
    }
    return Reading(fields=fields, document_url=None)


def test_parse_full_invoice_with_service_charge() -> None:
    """R-2.7, R-2.12: a UBL invoice yields the match fields and the document amounts."""
    reading = parse_invoice_xml(_invoice_xml().encode())

    assert reading.document_url is None
    assert {name: field.value for name, field in reading.fields.items()} == FULL_FIELDS
    assert all(field.source == "xml" for field in reading.fields.values())
    assert all(field.confidence == FULL_CONFIDENCE for field in reading.fields.values())


def test_parse_reduced_rate_invoice_without_charges() -> None:
    """R-2.7: a reduced-rate invoice reads its amounts, and absent optional amounts stay absent."""
    reading = parse_invoice_xml(
        _invoice_xml(
            base="45.16",
            igv="4.74",
            total="49.90",
            service_charge=None,
            discounts=None,
        ).encode()
    )

    assert reading.fields["base"].value == "45.16"
    assert reading.fields["igv"].value == "4.74"
    assert reading.fields["total"].value == "49.90"
    assert "service_charge" not in reading.fields
    assert "discounts" not in reading.fields


XXE_XML = b"""<?xml version="1.0"?>
<!DOCTYPE Invoice [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2">
  <cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
          >&xxe;</cbc:ID>
</Invoice>
"""

BILLION_LAUGHS_XML = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
  <!ENTITY lol5 "&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;&lol4;">
  <!ENTITY lol6 "&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;&lol5;">
  <!ENTITY lol7 "&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;&lol6;">
  <!ENTITY lol8 "&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;&lol7;">
  <!ENTITY lol9 "&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;&lol8;">
]>
<lolz>&lol9;</lolz>
"""

CREDIT_NOTE_XML = b"""<?xml version="1.0"?>
<CreditNote xmlns="urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2">
  <cbc:ID xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"
          >F123-1</cbc:ID>
</CreditNote>
"""

UTF16_INVOICE_XML = _invoice_xml().encode("utf-16")
ISO_8859_1_INVOICE_XML = (
    _invoice_xml().replace('encoding="UTF-8"', 'encoding="ISO-8859-1"').encode("iso-8859-1")
)

REJECTED_DOCUMENTS = [
    pytest.param(XXE_XML, XmlReason.DOCTYPE, id="doctype-external-entity"),
    pytest.param(BILLION_LAUGHS_XML, XmlReason.DOCTYPE, id="billion-laughs"),
    pytest.param(b"<!doctype Invoice><Invoice/>", XmlReason.DOCTYPE, id="lowercase-doctype"),
    pytest.param(UTF16_INVOICE_XML, XmlReason.DOCTYPE, id="utf-16-bom"),
    pytest.param(b"x" * (MAX_XML_BYTES + 1), XmlReason.TOO_LARGE, id="one-byte-over-two-megabytes"),
    pytest.param(
        b'<?xml version="1.0"?><Invoice><cbc:ID>F014-15160</cbc:ID>',
        XmlReason.MALFORMED,
        id="truncated",
    ),
    pytest.param(CREDIT_NOTE_XML, XmlReason.NOT_INVOICE, id="credit-note-root"),
]


@pytest.mark.parametrize(("data", "reason"), REJECTED_DOCUMENTS)
def test_parse_invoice_xml_rejects_unsafe_or_wrong_documents(data: bytes, reason: XmlReason) -> None:
    """R-2.12, BR-F09: unsafe, oversized, malformed and non-invoice XML names its reason."""
    with pytest.raises(XmlRejected) as excinfo:
        parse_invoice_xml(data)

    assert excinfo.value.reason is reason


def test_parse_invoice_xml_accepts_a_utf8_invoice() -> None:
    """R-2.12: an invoice declared and encoded as UTF-8 still parses."""
    reading = parse_invoice_xml(_invoice_xml().encode("utf-8"))

    assert reading.value("numero") == "15160"


def test_parse_invoice_xml_strips_leading_zeros_from_numero() -> None:
    """R-2.7: an ID F001-00000123 yields numero 123, the same value the other readers store."""
    reading = parse_invoice_xml(_invoice_xml(serie="F001", numero="00000123").encode())

    assert reading.value("numero") == "123"


def test_matches_invoice_ignores_zero_padding() -> None:
    """R-5.6: F014-15160 and F014-00015160 are the same invoice."""
    xml = parse_invoice_xml(_invoice_xml(numero="00015160").encode())

    assert matches_invoice(xml, _reading()) is True


def test_matches_invoice_rejects_an_underscore_in_the_number() -> None:
    """R-5.6: a numero with an underscore is not the same number as 15160."""
    xml = parse_invoice_xml(_invoice_xml(numero="1_5160").encode())

    assert matches_invoice(xml, _reading()) is False


@pytest.mark.parametrize(
    "invoice",
    [
        pytest.param(_reading(total="49.91"), id="other-total"),
        pytest.param(_reading(serie="F015"), id="other-serie"),
        pytest.param(_reading(ruc_emisor=SYNTHETIC_RUC_OTHER), id="other-issuer"),
        pytest.param(_reading(numero="15161"), id="other-numero"),
        pytest.param(_reading(total=None), id="missing-total"),
    ],
)
def test_matches_invoice_rejects_a_different_invoice(invoice: Reading) -> None:
    """R-5.6: another total, serie, issuer, numero or a missing field does not match."""
    xml = parse_invoice_xml(_invoice_xml().encode())

    assert matches_invoice(xml, invoice) is False


def test_matches_invoice_rejects_a_field_missing_from_both_readings() -> None:
    """R-5.6: an issuer absent on both sides is not a match."""
    xml = parse_invoice_xml(_invoice_xml(ruc_emisor="").encode())
    invoice = _reading(ruc_emisor=None)

    assert matches_invoice(xml, invoice) is False


def test_parse_invoice_xml_iso_8859_1_declaration_reads_like_utf8() -> None:
    """R-5.6: an ISO-8859-1 invoice, common in SUNAT XML, reads the same as its UTF-8 form."""
    assert parse_invoice_xml(ISO_8859_1_INVOICE_XML) == parse_invoice_xml(_invoice_xml().encode("utf-8"))


def test_parse_invoice_xml_ignores_a_backslash_schema_location_namespace() -> None:
    """R-2.12: a root namespace whose value is a relative Windows path is ignored."""
    without = _invoice_xml()
    with_schema_location = without.replace(
        '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"',
        '<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"\n'
        '         xmlns:schemaLocation="..\\x.xsd"',
    )
    assert with_schema_location != without

    reading = parse_invoice_xml(with_schema_location.encode())

    assert reading == parse_invoice_xml(without.encode())
