"""The SUNAT ``validarcomprobante`` call and the answer it carries (R-4.1).

Only a complete reading is validated; an incomplete one is a programming error.
The response is streamed under a small cap, and no failure text ever carries the
token or the body.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx

from invoicebot.config.models import ValidezApi
from invoicebot.extract.fields import Reading
from invoicebot.store.invoices import CpStatus
from invoicebot.sunat.token import BodyTooLarge, SunatUnavailable, read_capped

#: The ``estadoCp`` digits this client understands, as the strings SUNAT sends (R-4.1).
_VALID_ESTADO_CP = frozenset({"0", "1", "2", "3", "4"})

#: ``monto`` is sent with exactly two decimals (R-4.1).
_CENT = Decimal("0.01")

#: The date shape the validez API expects (R-4.1).
_FECHA_FORMAT = "%d/%m/%Y"


async def validate(
    http: httpx.AsyncClient,
    token: str,
    cfg: ValidezApi,
    owner_ruc: str,
    reading: Reading,
) -> CpStatus:
    """Ask SUNAT whether ``reading`` names a valid comprobante (R-4.1).

    Raises:
        ValueError: the reading lacks a value the request needs.
        SunatUnavailable: the API timed out, failed or answered unusably (R-4.1).
    """
    body = _request_json(reading)
    url = cfg.api_url.format(ruc=owner_ruc)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with http.stream("POST", url, json=body, headers=headers, timeout=cfg.timeout_s) as response:
            if response.status_code != httpx.codes.OK:
                raise SunatUnavailable(f"sunat validez request failed with status {response.status_code}")
            raw = await read_capped(response)
    except httpx.TimeoutException:
        raise SunatUnavailable("sunat validez request timed out") from None
    except httpx.HTTPError:
        raise SunatUnavailable("sunat validez request failed") from None
    except BodyTooLarge:
        raise SunatUnavailable("sunat validez response is too large") from None
    return _parse(raw)


def _request_json(reading: Reading) -> dict[str, str]:
    """Return the API's JSON body for ``reading`` (R-4.1).

    Raises:
        ValueError: the reading is incomplete; the caller validates only complete readings.
    """
    ruc = reading.value("ruc_emisor")
    tipo = reading.value("tipo")
    serie = reading.value("serie")
    numero = reading.value("numero")
    fecha = reading.fecha()
    total = reading.amount("total")
    if ruc is None or tipo is None or serie is None or numero is None:
        raise ValueError("reading must carry ruc_emisor, tipo, serie and numero")
    if fecha is None or total is None:
        raise ValueError("reading must carry fecha_emision and total")
    return {
        "numRuc": ruc,
        "codComp": tipo,
        "numeroSerie": serie,
        "numero": numero,
        "fechaEmision": fecha.strftime(_FECHA_FORMAT),
        "monto": str(total.quantize(_CENT)),
    }


def _parse(raw: bytes) -> CpStatus:
    """Return the status in a 200 body, or raise when the body is not the expected shape (R-4.1)."""
    try:
        payload = json.loads(raw)
    except ValueError:
        raise SunatUnavailable("sunat validez response is not valid JSON") from None
    if not isinstance(payload, dict):
        raise SunatUnavailable("sunat validez response is not a JSON object")
    if payload.get("success") is not True:
        raise SunatUnavailable("sunat validez response is not successful")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise SunatUnavailable("sunat validez response has no data object")
    return CpStatus(
        estado_cp=_estado_cp(data.get("estadoCp")),
        estado_ruc=_optional_text(data.get("estadoRuc")),
        cond_domi_ruc=_optional_text(data.get("condDomiRuc")),
    )


def _estado_cp(value: object) -> int:
    """Return ``estadoCp`` as a digit 0-4, or raise when it is anything else (R-4.1)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise SunatUnavailable("sunat validez response has an unusable estadoCp")
    text = str(value)
    if text not in _VALID_ESTADO_CP:
        raise SunatUnavailable("sunat validez response has an unusable estadoCp")
    return int(text)


def _optional_text(value: object) -> str | None:
    """Return ``value`` when it is a string, otherwise ``None`` (R-4.1)."""
    return value if isinstance(value, str) else None
