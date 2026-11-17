"""Tests for the SUNAT ``validarcomprobante`` call (R-4.1)."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from invoicebot.config.models import ValidezApi
from invoicebot.extract.fields import Field, Reading
from invoicebot.store.invoices import CpStatus
from invoicebot.sunat.token import MAX_RESPONSE_BYTES, SunatUnavailable
from invoicebot.sunat.validez import SunatUnauthorized, validate

# Both values come from tests/synthetic_rucs.txt; no real RUC appears in this file.
OWNER_RUC = "20000000001"
ISSUER_RUC = "51900000000"
BEARER = "synthetic-bearer-value"
API_URL = "https://sunat.example/v1/contribuyente/contribuyentes/{ruc}/validarcomprobante"

Handler = Callable[[httpx.Request], httpx.Response]


def _cfg() -> ValidezApi:
    """Return a validez config whose URL names the owner RUC at request time."""
    return ValidezApi(enabled=True, api_url=API_URL)


def _reading(
    *,
    fecha_emision: str | None = "2026-11-13",
    total: str | None = "118.50",
) -> Reading:
    """Build a complete reading, leaving ``fecha_emision`` or ``total`` out when ``None``."""
    fields = {
        "ruc_emisor": Field(value=ISSUER_RUC, source="qr", confidence=1.0),
        "tipo": Field(value="01", source="qr", confidence=1.0),
        "serie": Field(value="F001", source="qr", confidence=1.0),
        "numero": Field(value="123", source="qr", confidence=1.0),
    }
    if fecha_emision is not None:
        fields["fecha_emision"] = Field(value=fecha_emision, source="qr", confidence=1.0)
    if total is not None:
        fields["total"] = Field(value=total, source="ocr", confidence=1.0)
    return Reading(fields=fields, document_url=None)


def _answer(
    estado_cp: object = 1,
    *,
    success: bool = True,
    estado_ruc: object = "00",
    cond_domi_ruc: object = "00",
) -> dict[str, object]:
    """Return a validez response body around ``estado_cp``."""
    return {
        "success": success,
        "data": {
            "estadoCp": estado_cp,
            "estadoRuc": estado_ruc,
            "condDomiRuc": cond_domi_ruc,
        },
    }


def _reply(payload: object, status: int = 200) -> Handler:
    """Return a handler that answers every request with ``payload`` and ``status``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    return handler


def _timeout(request: httpx.Request) -> httpx.Response:
    """Raise the way a client past its deadline does."""
    raise httpx.ReadTimeout("sunat did not answer", request=request)


def _oversize(request: httpx.Request) -> httpx.Response:
    """Answer with one body byte past the cap."""
    return httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))


async def test_request_body_and_url_match_the_reading() -> None:
    """R-4.1: the POST names the owner in the URL and sends the reading with a bearer token."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_answer(1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == (
        "https://sunat.example/v1/contribuyente/contribuyentes/20000000001/validarcomprobante"
    )
    assert request.headers["Authorization"] == f"Bearer {BEARER}"
    assert json.loads(request.content) == {
        "numRuc": ISSUER_RUC,
        "codComp": "01",
        "numeroSerie": "F001",
        "numero": "123",
        "fechaEmision": "13/11/2026",
        "monto": "118.50",
    }


@pytest.mark.parametrize("estado_cp", [0, 1, 2, 3, 4])
async def test_each_estado_cp_parses(estado_cp: int) -> None:
    """R-4.1: each estadoCp digit 0-4 comes back as its int, with the issuer's codes."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_reply(_answer(estado_cp)))) as http:
        status = await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())

    assert status == CpStatus(estado_cp=estado_cp, estado_ruc="00", cond_domi_ruc="00")


async def test_string_estado_cp_is_accepted() -> None:
    """R-4.1: the string form of estadoCp parses to the same int."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(_reply(_answer("2")))) as http:
        status = await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())

    assert status.estado_cp == 2


async def test_non_string_issuer_codes_become_none() -> None:
    """R-4.1: an estadoRuc or condDomiRuc that is not a string is reported as missing."""
    body = _answer(1, estado_ruc=5, cond_domi_ruc=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(_reply(body))) as http:
        status = await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())

    assert status.estado_ruc is None
    assert status.cond_domi_ruc is None


@pytest.mark.parametrize(
    "handler",
    [
        _reply(_answer("7")),
        _reply(_answer(success=False)),
        _reply({}, status=500),
        _timeout,
        _oversize,
    ],
    ids=["estadoCp-7", "success-false", "http-500", "timeout", "oversize"],
)
async def test_unusable_answer_raises_sunat_unavailable(handler: Handler) -> None:
    """R-4.1: a bad estadoCp, a failed or slow request and an oversize body are unavailable."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnavailable):
            await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())


async def test_unauthorized_raises_sunat_unauthorized_without_the_token() -> None:
    """R-5.4: a 401 raises SunatUnauthorized whose message holds the status and no token."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_token", "echo": BEARER})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(SunatUnauthorized) as excinfo:
            await validate(http, BEARER, _cfg(), OWNER_RUC, _reading())

    message = str(excinfo.value)
    assert "401" in message
    assert BEARER not in message


async def test_incomplete_reading_raises_value_error() -> None:
    """R-4.1: only a complete reading is validated, so a missing total is a programming error."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        pytest.fail("an incomplete reading must not make a request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unreachable)) as http:
        with pytest.raises(ValueError, match="total"):
            await validate(http, BEARER, _cfg(), OWNER_RUC, _reading(total=None))
