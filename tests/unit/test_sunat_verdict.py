"""Tests for the SUNAT verdict (R-4.2, BR-V03)."""

from __future__ import annotations

import pytest

from invoicebot.store.padron import PadronRow
from invoicebot.sunat.validez import CpStatus
from invoicebot.sunat.verdict import decide

# From tests/synthetic_rucs.txt; no real RUC appears in this file.
ISSUER_RUC = "20000000001"


def _status(
    estado_cp: int = 1,
    *,
    estado_ruc: str | None = "00",
    cond_domi_ruc: str | None = "00",
) -> CpStatus:
    """Return a SUNAT answer for ``estado_cp`` with the issuer codes given."""
    return CpStatus(estado_cp=estado_cp, estado_ruc=estado_ruc, cond_domi_ruc=cond_domi_ruc)


def _padron(estado: str = "ACTIVO", condicion: str = "HABIDO") -> PadronRow:
    """Return a padrón row for the synthetic issuer RUC."""
    return PadronRow(ruc=ISSUER_RUC, razon_social="EMPRESA SINTETICA SAC", estado=estado, condicion=condicion)


@pytest.mark.parametrize(
    ("estado_cp", "expected"),
    [
        (None, ("BR-V04", False, True)),
        (0, ("BR-V01", False, True)),
        (1, (None, False, False)),
        (2, ("BR-V02", True, False)),
        (3, ("BR-V05", False, False)),
        (4, ("BR-V06", True, False)),
    ],
)
def test_decide_maps_estado_cp_to_its_rule(
    estado_cp: int | None, expected: tuple[str | None, bool, bool]
) -> None:
    """R-4.2: each estadoCp (or unavailable) gives its rule, ask and re-check."""
    status = None if estado_cp is None else _status(estado_cp)

    verdict = decide(status, None)

    assert (verdict.rule_id, verdict.ask, verdict.recheck) == expected
    assert verdict.issuer_line is None


def test_decide_reports_a_baja_definitiva_issuer() -> None:
    """BR-V03: estadoRuc 10 and condDomiRuc 00 give a BAJA DEFINITIVA/HABIDO line."""
    verdict = decide(_status(estado_ruc="10", cond_domi_ruc="00"), None)

    assert verdict.issuer_line == ("BAJA DEFINITIVA", "HABIDO")


def test_decide_omits_the_line_for_an_active_habido_issuer() -> None:
    """BR-V03: the 00/00 codes are ACTIVO/HABIDO, so there is no line."""
    verdict = decide(_status(estado_ruc="00", cond_domi_ruc="00"), None)

    assert verdict.issuer_line is None


def test_decide_falls_back_to_the_padron_without_a_status() -> None:
    """BR-V03: an unavailable SUNAT answer takes the issuer line from the padrón row."""
    verdict = decide(None, _padron(estado="BAJA DE OFICIO", condicion="HABIDO"))

    assert verdict.issuer_line == ("BAJA DE OFICIO", "HABIDO")


def test_decide_shows_an_unknown_issuer_code_as_is() -> None:
    """BR-V03: an unknown estadoRuc code 99 is shown unchanged."""
    verdict = decide(_status(estado_ruc="99", cond_domi_ruc="00"), None)

    assert verdict.issuer_line == ("99", "HABIDO")


def test_decide_treats_a_missing_issuer_code_as_ok() -> None:
    """BR-V03: a missing condDomiRuc counts as HABIDO, so only the estado shows."""
    verdict = decide(_status(estado_ruc="10", cond_domi_ruc=None), None)

    assert verdict.issuer_line == ("BAJA DEFINITIVA", "HABIDO")
