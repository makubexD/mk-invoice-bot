"""Tests for the portal probe issue comment (R-11.12, D193)."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from invoicebot.config.models import Portal
from invoicebot.fetchers.browser.probe_protocol import ProbedField, ProbeOutcome, ProbeReport
from invoicebot.github.probe_text import render_probe

URL = "https://portal.example.pe/consulta"

FULL_TEXT = """\
**Sonda del portal:** ok

| Etiqueta | Tipo | Dato |
| --- | --- | --- |
| Tipo de comprobante | select | tipo |
| Serie | text | serie |
| Numero | text | numero |
| Fecha de emision | date | fecha |
| Importe total | text | total |
| RUC emisor | text | ruc_emisor |
| Observacion | text | sin dato |

Borrador de `config/portals/<clave>.yaml`:

```yaml
url: https://portal.example.pe/consulta
adapter: generic_form
captcha: none
status: todo
fields:
  '#tipo':
    select: '{tipo_label}'
  '#serie': '{serie}'
  '#numero': '{numero}'
  '#fecha': '{fecha:%d/%m/%Y}'
  '#total': '{total:.2f}'
  '#ruc': '{ruc_emisor}'
submit: '#buscar'
downloads:
  pdf: '#pdf'
  xml: '#xml'
```"""


def _field(selector: str, kind: str, label: str, value_name: str | None) -> ProbedField:
    return ProbedField.model_validate(
        {"selector": selector, "kind": kind, "label": label, "value_name": value_name}
    )


def _full_report(**overrides: Any) -> ProbeReport:
    values: dict[str, Any] = {
        "outcome": ProbeOutcome.OK,
        "captcha": "none",
        "fields": [
            _field("#tipo", "select", "Tipo de comprobante", "tipo"),
            _field("#serie", "text", "Serie", "serie"),
            _field("#numero", "text", "Numero", "numero"),
            _field("#fecha", "date", "Fecha de emision", "fecha"),
            _field("#total", "text", "Importe total", "total"),
            _field("#ruc", "text", "RUC emisor", "ruc_emisor"),
            _field("#obs", "text", "Observacion", None),
        ],
        "submit": "#buscar",
        "pdf": "#pdf",
        "xml": "#xml",
    }
    values.update(overrides)
    return ProbeReport.model_validate(values)


def _draft(text: str) -> dict[str, Any]:
    block = text.split("```yaml\n", 1)[1].split("\n```", 1)[0]
    loaded = yaml.safe_load(block)
    assert isinstance(loaded, dict)
    return loaded


def test_full_report_renders_the_exact_text() -> None:
    """R-11.12, D193: a full report gives the outcome, the field table and the draft."""
    assert render_probe(URL, _full_report()) == FULL_TEXT


def test_captcha_report_without_submit_has_no_submit_key() -> None:
    """R-11.12, D193: a captcha report with no submit button keeps ``captcha: image`` and omits ``submit``."""
    report = _full_report(outcome=ProbeOutcome.CAPTCHA, captcha="image", submit=None, pdf=None, xml=None)
    text = render_probe(URL, report)

    assert text is not None
    draft = _draft(text)
    assert draft["captcha"] == "image"
    assert "submit" not in draft
    assert "downloads" not in draft


def test_url_query_is_reduced() -> None:
    """R-11.12, D193: a query, credentials and a fragment never reach the text."""
    text = render_probe("https://user:pass@portal.example.pe/consulta?token=abc#top", _full_report())

    assert text is not None
    assert _draft(text)["url"] == URL
    assert "token" not in text
    assert "pass" not in text


@pytest.mark.parametrize("url", ["http://portal.example.pe/x", "https://portal.example.pe/a b", ""])
def test_a_url_that_fails_is_refused(url: str) -> None:
    """R-11.12, D193: a URL the reducer refuses gives no text."""
    assert render_probe(url, _full_report()) is None


def test_label_has_pipe_backtick_and_at_removed() -> None:
    """R-11.12, D193: ``|``, backticks and ``@`` never reach a table cell, even past the wire validation."""
    bad = ProbedField.model_construct(
        selector="#serie", kind="text", label="Se|rie `x` @y", value_name="serie"
    )
    report = _full_report().model_copy(update={"fields": (bad,)})
    text = render_probe(URL, report)

    assert text is not None
    assert "| Serie x y | text | serie |" in text
    assert "@" not in text
    assert "`x`" not in text


def test_the_draft_loads_as_a_portal_once_a_key_is_added() -> None:
    """R-11.12, D193: the draft is a valid Portal with a key added."""
    text = render_probe(URL, _full_report())

    assert text is not None
    portal = Portal.model_validate({"key": "portal-example", **_draft(text)})
    assert portal.adapter == "generic_form"
    assert portal.submit == "#buscar"
    assert portal.downloads is not None
    assert portal.downloads.pdf == "#pdf"
    assert set(portal.fields) == {"#tipo", "#serie", "#numero", "#fecha", "#total", "#ruc"}


def test_a_field_with_no_value_name_is_in_the_table_but_not_the_draft() -> None:
    """R-11.12, D193: ``sin dato`` fields are listed, not templated."""
    text = render_probe(URL, _full_report())

    assert text is not None
    assert "| Observacion | text | sin dato |" in text
    assert "#obs" not in _draft(text)["fields"]
