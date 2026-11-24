"""Parse and apply the ``campo=valor`` edits a sender types in chat (R-3.5, D97).

An edit line names one invoice field and its new value. Aliases shorten the field's
name, ``motivo``/``descripcion`` stay with :mod:`invoicebot.pipeline.overrides`, and
each value is normalised here so the caller stores a canonical reading field. A value
the field's rule rejects becomes an :class:`EditError` that names the business rule
when there is one; the caller answers with the first error and changes nothing.
:func:`apply_edits` stores the accepted fields on the invoice the message targets and
re-routes it, so the route handler validates the new values and sends the summary again;
a ``PENDING_USER`` invoice is unparked first (D94).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Any

from psycopg import AsyncConnection

from invoicebot.channels.base import InboundMessage
from invoicebot.extract.fields import FULL_CONFIDENCE, Field, Reading
from invoicebot.extract.validate import RULE_BAD_DATE, RULE_BAD_RUC, check_serie, is_valid_ruc
from invoicebot.pipeline.extract import ROUTE_STEP, RULE_UNREADABLE
from invoicebot.pipeline.outgoing import Addressee, queue_message
from invoicebot.pipeline.overrides import (
    MAX_OVERRIDE_CHARS,
    OVERRIDE_KEYS,
    edit_target,
    without_accents,
)
from invoicebot.queue.jobs import enqueue
from invoicebot.rules.messages_es import EDIT_INVALID, EDIT_LATE, MESSAGES, state_desc
from invoicebot.states import EDITABLE, EXTRACT_FAILED, PENDING_USER, ROUTING
from invoicebot.store.flow_b import load_flow_data, merge_flow_data
from invoicebot.store.invoices import (
    clear_editing,
    get_state_rule,
    load_reading,
    save_reading,
    set_state,
    unpark,
)
from invoicebot.text import replace_unsafe_chars

#: The most edit lines one message may carry; later lines each get an error.
MAX_EDIT_LINES = 10
#: Short names a sender may use for a field (R-3.5, D97).
ALIASES: Mapping[str, str] = {
    "ruc": "ruc_emisor",
    "fecha": "fecha_emision",
    "cliente": "num_doc_adq",
}
#: The document type implied by a serie's first letter (R-3.5, D97).
TIPO_BY_SERIE_PREFIX: Mapping[str, str] = {"F": "01", "B": "03"}
#: The stored routing answers (D92) each edited field invalidates: the flow, the buyer
#: check and the cap follow the buyer, the brand the issuer and serie, the cap the total (R-3.5).
STALE_ANSWERS: Mapping[str, frozenset[str]] = {
    "num_doc_adq": frozenset({"flow", "buyer_ok", "cap_ok"}),
    "ruc_emisor": frozenset({"comercio"}),
    "serie": frozenset({"comercio"}),
    "total": frozenset({"cap_ok"}),
}

_DATE_PATTERN = re.compile(r"([0-9]{2})[/-]([0-9]{2})[/-]([0-9]{4})")
#: At most 10 integer digits, since ``invoices.total`` is ``NUMERIC(12,2)`` (R-3.5).
_TOTAL_PATTERN = re.compile(r"[0-9]{1,10}(?:[.,][0-9]{1,2})?")
#: Where a further ``key=value`` pair starts after whitespace in one edit line (D188).
_PAIR_START_PATTERN = re.compile(r"\s+(\S+?)=")

_Validator = Callable[[str], tuple[str | None, str | None]]


@dataclass(frozen=True)
class EditError:
    """One edit line the parse rejected, with the rule and field it concerns (R-3.5)."""

    rule_id: str | None
    line: str
    field: str | None
    value: str


@dataclass(frozen=True)
class Edits:
    """The canonical fields and the rejected lines of one edit message (R-3.5, D97)."""

    fields: Mapping[str, str]
    errors: tuple[EditError, ...]


def parse_edits(text: str) -> Edits:
    """Return the fields and errors of the ``campo=valor`` lines in ``text`` (R-3.5, D97, D188).

    A line without ``=`` is ignored. Keys are case-insensitive and accent-free after
    ``strip``; ``motivo`` and ``descripcion`` are skipped. A line whose first key is
    known may carry several pairs, each starting at a known key after whitespace, and
    each pair counts as one assignment (D188). Assignments past :data:`MAX_EDIT_LINES`
    and values longer than :data:`MAX_OVERRIDE_CHARS` are errors. A serie without a
    well-formed ``tipo`` gets one from its first letter.
    """
    fields: dict[str, str] = {}
    errors: list[EditError] = []
    assignments = 0
    for raw in text.splitlines():
        if "=" not in raw:
            continue
        for line in _split_pairs(raw.strip()):
            assignments += 1
            key, _, raw_value = line.partition("=")
            name = _canonical_key(key)
            value = raw_value.strip()
            if name in OVERRIDE_KEYS:
                continue
            if assignments > MAX_EDIT_LINES or len(value) > MAX_OVERRIDE_CHARS:
                errors.append(EditError(None, line, name, value))
            elif name is None:
                errors.append(EditError(None, line, None, value))
            else:
                _apply(fields, errors, name, line, value)
    _infer_tipo(fields)
    return Edits(MappingProxyType(fields), tuple(errors))


def _split_pairs(line: str) -> list[str]:
    """Return the ``key=value`` pair texts of one stripped edit line (D188).

    A line whose first key is unknown is one pair, so it fails validation as a whole; a
    free-text key (``motivo``, ``descripcion``) keeps the rest of the line as its value.
    Otherwise a further pair starts after whitespace at a word :func:`_canonical_key`
    knows followed by ``=``, and its value runs up to the next pair's start.
    """
    first_key = _canonical_key(line.partition("=")[0])
    if first_key is None or first_key in OVERRIDE_KEYS:
        return [line]
    starts = [0]
    for match in _PAIR_START_PATTERN.finditer(line):
        if _canonical_key(match.group(1)) is not None:
            starts.append(match.start())
    pairs: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(line)
        pairs.append(line[start:end].strip())
    return pairs


async def apply_edits(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    edits: Edits,
) -> int | None:
    """Store accepted edits on their target invoice and send it back to routing (R-3.5, D97).

    The target is the message's edit target (the quoted invoice, else the profile's newest
    open one). No target changes nothing and returns None. A rejected line gets the first
    error's text and changes nothing. A ``PENDING_USER`` invoice is unparked first, so the
    edit applies to the state it was waiting in (D94). A state outside ``EDITABLE`` gets
    ``EDIT_LATE``. From ``EXTRACT_FAILED`` a reading still missing a required field gets
    BR-E01 and stays; any other accepted edit stores its fields with source ``user``,
    drops the stored answers that depended on an edited field (so routing asks only those
    again, R-3.5) and moves to ``ROUTING`` with a ``route`` job, all in the caller's
    transaction. An [Editar] pin on the invoice is cleared once its fields are stored (D90).
    """
    invoice_id = await edit_target(conn, msg)
    if invoice_id is None:
        return None
    current = await get_state_rule(conn, invoice_id)
    if current is None:
        raise RuntimeError(f"invoice {invoice_id} does not exist")
    state, _ = current
    if edits.errors:
        await _reply_error(conn, msg, invoice_id, edits.errors[0])
        return invoice_id
    if state == PENDING_USER:
        await unpark(conn, invoice_id)
        current = await get_state_rule(conn, invoice_id)
        if current is None:
            raise RuntimeError(f"invoice {invoice_id} does not exist")
        state, _ = current
    if state not in EDITABLE:
        await _reply(
            conn,
            msg,
            invoice_id,
            EDIT_LATE.format(id=invoice_id, state_desc=state_desc(state)),
        )
        return invoice_id
    reading = _edited_reading(await load_reading(conn, invoice_id), edits.fields)
    await save_reading(conn, invoice_id, reading)
    await _drop_stale_answers(conn, invoice_id, edits.fields)
    if state == EXTRACT_FAILED and reading.missing_required():
        # The pin stays: BR-E01 asks for more fields of this same invoice (D90).
        await _reply(conn, msg, invoice_id, MESSAGES[RULE_UNREADABLE], rule_id=RULE_UNREADABLE)
        return invoice_id
    await clear_editing(conn, invoice_id)
    await set_state(conn, invoice_id, ROUTING, expected=state)
    await enqueue(conn, invoice_id, ROUTE_STEP)
    return invoice_id


async def _drop_stale_answers(
    conn: AsyncConnection[Any],
    invoice_id: int,
    edited: Mapping[str, str],
) -> None:
    """Remove the routing answers an edited field invalidates; keep the rest (R-3.5, D92)."""
    stale = frozenset().union(*(STALE_ANSWERS.get(name, frozenset()) for name in edited))
    stored = (await load_flow_data(conn, invoice_id)).get("answers")
    if not stale or not isinstance(stored, dict):
        return
    kept = {key: value for key, value in stored.items() if key not in stale}
    await merge_flow_data(conn, invoice_id, {"answers": kept})


def _edited_reading(reading: Reading | None, edits: Mapping[str, str]) -> Reading:
    """Return the stored reading with each edited field given source ``user`` (D97).

    An invoice with no stored reading, such as one in ``EXTRACT_FAILED`` for an unreadable
    photo, starts from an empty reading and keeps no document URL.
    """
    base = Reading(fields={}, document_url=None) if reading is None else reading
    user_fields = {name: Field(value, "user", FULL_CONFIDENCE) for name, value in edits.items()}
    return Reading(fields={**base.fields, **user_fields}, document_url=base.document_url)


def error_text(error: EditError) -> str:
    """Return the reply text for one rejected edit line (BR-E02, BR-E03, R-3.5, R-12.8).

    The invalid-line reply echoes the line with its unsafe characters replaced and
    capped at :data:`MAX_OVERRIDE_CHARS`, so untrusted text cannot carry controls into
    the answer (R-12.8).
    """
    if error.rule_id == RULE_BAD_RUC:
        return MESSAGES[RULE_BAD_RUC].format(ruc=_echo(error.value))
    if error.rule_id == RULE_BAD_DATE:
        return MESSAGES[RULE_BAD_DATE].format(fecha=_echo(error.value))
    return EDIT_INVALID.format(line=_echo(error.line))


def _echo(text: str) -> str:
    """Return untrusted ``text`` with unsafe characters replaced, capped for a reply (R-12.8)."""
    return replace_unsafe_chars(text)[:MAX_OVERRIDE_CHARS]


async def _reply_error(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    invoice_id: int,
    error: EditError,
) -> None:
    """Queue the text for the first rejected edit line (BR-E02, BR-E03, R-3.5)."""
    await _reply(conn, msg, invoice_id, error_text(error), rule_id=error.rule_id)


async def _reply(
    conn: AsyncConnection[Any],
    msg: InboundMessage,
    invoice_id: int,
    text: str,
    *,
    rule_id: str | None = None,
) -> None:
    """Queue one reply to the message's sender in the caller's transaction."""
    await queue_message(
        conn,
        Addressee(msg.channel, msg.profile_id),
        text,
        invoice_id=invoice_id,
        rule_id=rule_id,
    )


def _apply(
    fields: dict[str, str],
    errors: list[EditError],
    name: str,
    line: str,
    value: str,
) -> None:
    """Validate one line's value and store it, or record its error (R-3.5)."""
    normalized, rule_id = _VALIDATORS[name](value)
    if normalized is None:
        errors.append(EditError(rule_id, line, name, value))
    else:
        fields[name] = normalized


def _infer_tipo(fields: dict[str, str]) -> None:
    """Add ``tipo`` from the serie's first letter when the sender did not give one (D97)."""
    serie = fields.get("serie")
    if serie is None or "tipo" in fields:
        return
    tipo = TIPO_BY_SERIE_PREFIX.get(serie[0])
    if tipo is not None:
        fields["tipo"] = tipo


def _canonical_key(key: str) -> str | None:
    """Return the canonical field name for ``key``, or None when the key is unknown (D97)."""
    name = without_accents(key.strip().lower())
    if name in OVERRIDE_KEYS or name in _VALIDATORS:
        return name
    return ALIASES.get(name)


def _ruc(value: str) -> tuple[str | None, str | None]:
    """Return the RUC, or BR-E02 when its check digit fails (BR-E02)."""
    if not is_valid_ruc(value):
        return None, RULE_BAD_RUC
    return value, None


def _num_doc(value: str) -> tuple[str | None, str | None]:
    """Return the buyer document, accepted at 8 or 11 digits (R-3.5)."""
    if len(value) in (8, 11) and value.isascii() and value.isdigit():
        return value, None
    return None, None


def _fecha(value: str) -> tuple[str | None, str | None]:
    """Return the date in ISO form, or BR-E03 when it is malformed or impossible (BR-E03)."""
    match = _DATE_PATTERN.fullmatch(value)
    if match is None:
        return None, RULE_BAD_DATE
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day).isoformat(), None
    except ValueError:
        return None, RULE_BAD_DATE


def _total(value: str) -> tuple[str | None, str | None]:
    """Return the amount with a dot, at most two decimals, or an error (R-3.5)."""
    if _TOTAL_PATTERN.fullmatch(value) is None:
        return None, None
    return value.replace(",", "."), None


def _serie(value: str) -> tuple[str | None, str | None]:
    """Return the serie upper-cased, or an error when it does not match (R-2.4)."""
    upper = value.upper()
    if not check_serie(upper):
        return None, None
    return upper, None


def _numero(value: str) -> tuple[str | None, str | None]:
    """Return the 1-8 digit number without leading zeros, or an error (R-3.5)."""
    if not 1 <= len(value) <= 8 or not value.isascii() or not value.isdigit():
        return None, None
    return value.lstrip("0") or "0", None


def _tipo(value: str) -> tuple[str | None, str | None]:
    """Return the document type, accepted only as 01 or 03 (R-3.5)."""
    if value in ("01", "03"):
        return value, None
    return None, None


_VALIDATORS: Mapping[str, _Validator] = {
    "ruc_emisor": _ruc,
    "num_doc_adq": _num_doc,
    "fecha_emision": _fecha,
    "total": _total,
    "serie": _serie,
    "numero": _numero,
    "tipo": _tipo,
}
