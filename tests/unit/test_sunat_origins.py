"""Tests for the hard-coded SUNAT origins and the SOL site file (R-9.10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from invoicebot.config.models import SunatSolSite
from invoicebot.sunat_origins import SUNAT_ORIGINS, is_sunat_origin

ROOT = Path(__file__).resolve().parents[2]
SUNAT_SOL = ROOT / "config" / "sunat_sol.yaml"


@pytest.mark.parametrize("url", sorted(SUNAT_ORIGINS))
def test_each_sunat_origin_is_accepted(url: str) -> None:
    """R-9.10: both hard-coded SUNAT origins let the credentials be typed."""
    assert is_sunat_origin(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://api-seguridad.sunat.gob.pe",
        "https://api-seguridad.sunat.gob.pe.evil.test",
        "https://evil.test/?u=https://e-menu.sunat.gob.pe",
        "https://user@e-menu.sunat.gob.pe",
        "https://e-menu.sunat.gob.pe:8443",
    ],
)
def test_urls_outside_a_sunat_origin_are_refused(url: str) -> None:
    """R-9.10: plain http, a look-alike host, embedded userinfo or another port is refused."""
    assert not is_sunat_origin(url)


def test_shipped_site_file_loads_as_todo() -> None:
    """R-9.10: the shipped config/sunat_sol.yaml validates and stays todo until S0.4."""
    document = yaml.safe_load(SUNAT_SOL.read_text(encoding="utf-8"))
    site = SunatSolSite.model_validate(document)
    assert site.status == "todo"


def test_site_refuses_a_login_outside_sunat() -> None:
    """R-9.10: a login.url outside the hard-coded origins fails at config load."""
    document: dict[str, Any] = {
        "status": "todo",
        "login": {
            "url": "https://api-seguridad.sunat.gob.pe.evil.test/login",
            "ruc": "#ruc",
            "user": "#usuario",
            "password": "#clave",
            "submit": "#btnAceptar",
        },
        "query": {"key": "sunat_sol", "adapter": "generic_form"},
    }
    with pytest.raises(ValidationError):
        SunatSolSite.model_validate(document)
