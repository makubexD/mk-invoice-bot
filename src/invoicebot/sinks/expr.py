"""The small safe template language shared by the delivery sinks (spec 03 §7).

One job: validate and render the field syntax used for folder names, file names, cells, subject
and body, and list the names a template needs. A field is a bare name, or ``name.attr`` reading
one key of a mapping value; indexing, conversions, nested replacement fields, arbitrary
attribute access and unknown names are refused with :class:`ExprError`.
"""

from __future__ import annotations

import string
from collections.abc import Mapping, Sequence


class ExprError(ValueError):
    """A template uses a form the safe field language does not allow (R-7.1, R-7.6)."""


class _Formatter(string.Formatter):
    """A formatter that reads fields from a mapping and rejects unsafe field forms."""

    def get_field(
        self, field_name: str, _args: Sequence[object], kwargs: Mapping[str, object]
    ) -> tuple[object, str]:
        """Return the value for one validated field, or raise ``ExprError``."""
        name, attr = _parse_field(field_name)
        if name not in kwargs:
            raise ExprError(f"unknown name {name!r} in {{{field_name}}}")
        value = kwargs[name]
        if attr is None:
            return value, name
        if not isinstance(value, Mapping):
            raise ExprError(f"attribute access on a non-mapping {name!r} in {{{field_name}}}")
        if attr not in value:
            raise ExprError(f"unknown attribute {attr!r} in {{{field_name}}}")
        return value[attr], name


_FORMATTER = _Formatter()


def render(template: str, values: Mapping[str, object]) -> str:
    """Return ``template`` with every field replaced from ``values`` (R-7.1, R-7.2, R-7.6).

    A ``date`` value accepts ``strftime`` specs (``{fecha:%Y}``) and a ``Decimal`` the normal
    format spec, because both provide ``__format__``.
    """
    _validate(template)
    return _FORMATTER.vformat(template, (), values)


def field_names(template: str) -> frozenset[str]:
    """Return the top-level names ``template`` uses (``profile.legal_name`` -> ``profile``)."""
    return frozenset(name for name, _ in _validate(template))


def _validate(template: str) -> list[tuple[str, str | None]]:
    """Return the validated ``(name, attr)`` of every field in ``template``.

    This is the one parse path shared by :func:`render` and :func:`field_names`, so both reject
    the same forbidden forms.
    """
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise ExprError(str(exc)) from None
    fields: list[tuple[str, str | None]] = []
    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if conversion is not None:
            raise ExprError(f"conversions are not allowed in {{{field_name}!{conversion}}}")
        _reject_nested_field(field_name, format_spec or "")
        fields.append(_parse_field(field_name))
    return fields


def _reject_nested_field(field_name: str, format_spec: str) -> None:
    """Raise ``ExprError`` when the format spec holds a replacement field of its own."""
    try:
        nested = list(_FORMATTER.parse(format_spec))
    except ValueError as exc:
        raise ExprError(str(exc)) from None
    if any(nested_name is not None for _, nested_name, _, _ in nested):
        raise ExprError(f"nested replacement fields are not allowed in {{{field_name}}}")


def _parse_field(field_name: str) -> tuple[str, str | None]:
    """Return ``(name, attr)`` for one field, raising ``ExprError`` on a forbidden form."""
    if "[" in field_name:
        raise ExprError(f"indexing is not allowed in {{{field_name}}}")
    parts = field_name.split(".")
    if len(parts) > 2:
        raise ExprError(f"only one attribute level is allowed in {{{field_name}}}")
    name = parts[0]
    if not name.isidentifier():
        raise ExprError(f"invalid field name in {{{field_name}}}")
    if len(parts) == 1:
        return name, None
    attr = parts[1]
    if not attr.isidentifier() or attr.startswith("_"):
        raise ExprError(f"invalid attribute in {{{field_name}}}")
    return name, attr
