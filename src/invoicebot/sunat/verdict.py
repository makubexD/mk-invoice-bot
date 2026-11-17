"""The verdict a SUNAT answer leads to: rule, question and re-check (R-4.2, D125).

The pipeline asks once per confirmed invoice and acts on the returned rule: a
re-check schedules a later ``sunat_recheck`` job, a question moves the invoice to
``CONFIRM_INVALID``, and the issuer line adds BR-V03 when the RUC is not
``ACTIVO``/``HABIDO``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from invoicebot.store.invoices import CpStatus
from invoicebot.store.padron import PadronRow

#: The invoice's comprobante does not exist yet in SUNAT (R-4.2).
RULE_MISSING = "BR-V01"

#: SUNAT says the comprobante is annulled (R-4.2).
RULE_ANNULLED = "BR-V02"

#: The issuer's RUC is not ACTIVO/HABIDO; a warning line, not a stop (BR-V03).
RULE_ISSUER_STATE = "BR-V03"

#: SUNAT could not be asked: continue unvalidated and re-check later (R-4.2).
RULE_UNAVAILABLE = "BR-V04"

#: SUNAT says the comprobante is authorized, non-electronic (R-4.2).
RULE_AUTHORIZED = "BR-V05"

#: SUNAT says the comprobante is not authorized (R-4.2).
RULE_NOT_AUTHORIZED = "BR-V06"

#: The ``estadoRuc`` codes and the names they stand for (BR-V03).
_ESTADO_RUC: Mapping[str, str] = MappingProxyType(
    {
        "00": "ACTIVO",
        "01": "BAJA PROVISIONAL",
        "02": "BAJA PROV. POR OFICIO",
        "03": "SUSPENSION TEMPORAL",
        "10": "BAJA DEFINITIVA",
        "11": "BAJA DE OFICIO",
        "22": "INHABILITADO-VENT.UNICA",
    }
)

#: The ``condDomiRuc`` codes and the names they stand for (BR-V03).
_COND_DOMI_RUC: Mapping[str, str] = MappingProxyType(
    {
        "00": "HABIDO",
        "09": "PENDIENTE",
        "11": "POR VERIFICAR",
        "12": "NO HABIDO",
        "20": "NO HALLADO",
    }
)

#: The estado and condición an issuer must have to skip BR-V03 (BR-V03).
ESTADO_OK = "ACTIVO"
CONDICION_OK = "HABIDO"

#: ``estadoCp`` (or ``None`` when SUNAT was unavailable) → rule, ask, re-check (R-4.2, D125).
_DECISIONS: Mapping[int | None, tuple[str | None, bool, bool]] = MappingProxyType(
    {
        None: (RULE_UNAVAILABLE, False, True),
        0: (RULE_MISSING, False, True),
        1: (None, False, False),
        2: (RULE_ANNULLED, True, False),
        3: (RULE_AUTHORIZED, False, False),
        4: (RULE_NOT_AUTHORIZED, True, False),
    }
)


@dataclass(frozen=True)
class Verdict:
    """What to do with one SUNAT answer (R-4.2).

    ``rule_id`` is the rule to report, or ``None`` when the answer needs no
    message. ``ask`` moves the invoice to ``CONFIRM_INVALID``. ``recheck``
    schedules a later ``sunat_recheck``. ``issuer_line`` carries BR-V03's
    ``(estado, condición)`` when the issuer is not ``ACTIVO``/``HABIDO``.
    """

    rule_id: str | None
    ask: bool
    recheck: bool
    issuer_line: tuple[str, str] | None


def decide(status: CpStatus | None, padron: PadronRow | None) -> Verdict:
    """Return the verdict for ``status``, or for its absence (R-4.2, D125).

    ``None`` means SUNAT was unavailable. ``padron`` is used for BR-V03 only when
    the answer carries no issuer state.
    """
    estado_cp = None if status is None else status.estado_cp
    rule_id, ask, recheck = _DECISIONS[estado_cp]
    return Verdict(
        rule_id=rule_id,
        ask=ask,
        recheck=recheck,
        issuer_line=_issuer_line(status, padron),
    )


def _issuer_line(status: CpStatus | None, padron: PadronRow | None) -> tuple[str, str] | None:
    """Return the issuer's ``(estado, condición)`` when it is abnormal (BR-V03)."""
    if status is not None and (status.estado_ruc is not None or status.cond_domi_ruc is not None):
        estado = _code_name(_ESTADO_RUC, status.estado_ruc, ESTADO_OK)
        condicion = _code_name(_COND_DOMI_RUC, status.cond_domi_ruc, CONDICION_OK)
        return _abnormal(estado, condicion)
    if padron is not None:
        return _abnormal(padron.estado, padron.condicion)
    return None


def _code_name(table: Mapping[str, str], code: str | None, missing: str) -> str:
    """Return the name for ``code``; an unknown code shows itself, a missing one the OK name."""
    if code is None:
        return missing
    return table.get(code, code)


def _abnormal(estado: str, condicion: str) -> tuple[str, str] | None:
    """Return ``(estado, condicion)`` unless it is the ACTIVO/HABIDO pair (BR-V03)."""
    return None if (estado, condicion) == (ESTADO_OK, CONDICION_OK) else (estado, condicion)
