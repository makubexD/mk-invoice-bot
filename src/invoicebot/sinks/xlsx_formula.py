"""The small Excel formula subset used to recompute dependent cells (D87, R-7.4).

One job: parse a formula made of numbers, cell references, ranges, ``+ - * /``, parentheses and the
functions MIN, MAX and SUM; list the cells it reads; and evaluate it against known cell values.
Everything outside that subset is refused with :class:`UnsupportedFormula`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

_MAX_RANGE_CELLS = 1000
_MAX_COLUMN = 16384  # XFD, the last column Excel supports
MAX_NESTING = 64  # the deepest parentheses or function calls Excel accepts
_FUNCTIONS = frozenset({"MAX", "MIN", "SUM"})

_TOKEN_RE = re.compile(
    r"(?P<space>\s+)"
    r"|(?P<number>\d+(?:\.\d*)?|\.\d+)"
    r"|(?P<ref>\$?[A-Za-z]{1,3}\$?\d+)"
    r"|(?P<name>[A-Za-z]+)"
    r"|(?P<op>[+\-*/(),:])",
    re.IGNORECASE,
)

_REFUSED_CHARACTERS = {
    "!": "sheet-qualified references are not supported",
    '"': "string literals are not supported",
    "%": "the % operator is not supported",
    "^": "the ^ operator is not supported",
    "<": "comparisons are not supported",
    ">": "comparisons are not supported",
    "=": "comparisons are not supported",
    "&": "the & operator is not supported",
}


class UnsupportedFormula(ValueError):
    """A formula uses a form outside the supported subset (D87, R-7.4)."""


@dataclass(frozen=True)
class Number:
    """A literal number."""

    value: Decimal


@dataclass(frozen=True)
class Ref:
    """A single cell reference, normalized to upper case without ``$``."""

    cell: str


@dataclass(frozen=True)
class Range:
    """A rectangle of cells, allowed only as a function argument."""

    cells: tuple[str, ...]


@dataclass(frozen=True)
class Unary:
    """A unary minus."""

    operand: Node


@dataclass(frozen=True)
class Binary:
    """An arithmetic operation on two values."""

    operator: str
    left: Node
    right: Node


@dataclass(frozen=True)
class Call:
    """A call to MIN, MAX or SUM."""

    name: str
    args: tuple[Node, ...]


Node = Number | Ref | Range | Unary | Binary | Call


@dataclass(frozen=True)
class Formula:
    """A parsed formula that can list its cells and be evaluated (D87, R-7.4)."""

    root: Node

    def refs(self) -> frozenset[str]:
        """Return every cell the formula reads, with ranges expanded."""
        return _refs(self.root)

    def evaluate(self, values: Mapping[str, Decimal]) -> Decimal:
        """Return the formula's value, reading a missing cell as zero as Excel does."""
        return _evaluate(self.root, values)


def parse(formula: str) -> Formula:
    """Return ``formula`` parsed, or raise ``UnsupportedFormula`` outside the subset (R-7.4)."""
    return Formula(_Parser(_tokenize(formula)).parse())


def range_refs(start: str, end: str) -> frozenset[str]:
    """Return every cell between two references, raising ``UnsupportedFormula`` past the 1000-cell cap."""
    return frozenset(_range(start, end).cells)


def _refs(node: Node) -> frozenset[str]:
    """Return every cell ``node`` reads."""
    match node:
        case Number():
            return frozenset()
        case Ref():
            return frozenset({node.cell})
        case Range():
            return frozenset(node.cells)
        case Unary():
            return _refs(node.operand)
        case Binary():
            return _refs(node.left) | _refs(node.right)
        case Call():
            cells: set[str] = set()
            for arg in node.args:
                cells.update(_refs(arg))
            return frozenset(cells)


def _evaluate(node: Node, values: Mapping[str, Decimal]) -> Decimal:
    """Return the value of ``node``; a cell absent from ``values`` reads as zero."""
    match node:
        case Number():
            return node.value
        case Ref():
            return values.get(node.cell, Decimal(0))
        case Unary():
            return -_evaluate(node.operand, values)
        case Binary():
            return _apply(node.operator, _evaluate(node.left, values), _evaluate(node.right, values))
        case Call():
            return _call(node.name, node.args, values)
        case Range():
            raise UnsupportedFormula("a range can only be a function argument")


def _apply(operator: str, left: Decimal, right: Decimal) -> Decimal:
    """Return ``left operator right``, refusing division by zero."""
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if right == 0:
        raise UnsupportedFormula("division by zero")
    return left / right


def _call(name: str, args: Sequence[Node], values: Mapping[str, Decimal]) -> Decimal:
    """Return the value of a MIN, MAX or SUM call.

    A range contributes only the cells present in ``values``: Excel skips empty cells in a range,
    so ``MIN(A1:A3)`` with ``A3`` empty is the smaller of ``A1`` and ``A2``, not zero.
    """
    numbers: list[Decimal] = []
    for arg in args:
        if isinstance(arg, Range):
            numbers.extend(values[cell] for cell in arg.cells if cell in values)
        else:
            numbers.append(_evaluate(arg, values))
    if name == "SUM":
        return sum(numbers, Decimal(0))
    if not numbers:
        return Decimal(0)
    if name == "MIN":
        return min(numbers)
    return max(numbers)


@dataclass(frozen=True)
class _Token:
    """One lexical token of a formula."""

    kind: str
    text: str


def _tokenize(formula: str) -> list[_Token]:
    """Return the tokens of ``formula``, refusing any character outside the subset."""
    text = formula.strip()
    if text.startswith("="):
        text = text[1:]
    tokens: list[_Token] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            raise _refused_character(text[position], formula)
        if match.group("space") is None:
            kind = match.lastgroup
            if kind is None:
                raise UnsupportedFormula(f"invalid token {match.group()!r} in formula {formula!r}")
            tokens.append(_Token(kind, match.group()))
        position = match.end()
    return tokens


def _refused_character(character: str, formula: str) -> UnsupportedFormula:
    """Return the refusal for a character the tokenizer does not know."""
    reason = _REFUSED_CHARACTERS.get(character)
    if reason is not None:
        return UnsupportedFormula(f"{reason} in formula {formula!r}")
    return UnsupportedFormula(f"unsupported character {character!r} in formula {formula!r}")


class _Parser:
    """A recursive-descent parser over the tokens of one formula."""

    def __init__(self, tokens: Sequence[_Token]) -> None:
        self._tokens = tokens
        self._position = 0
        self._depth = 0

    def parse(self) -> Node:
        """Return the expression, refusing trailing tokens."""
        node = self._expression()
        token = self._peek()
        if token is not None:
            if token.text == ":":
                raise UnsupportedFormula("a range is only allowed inside a function argument")
            raise UnsupportedFormula(f"unexpected {token.text!r} after the expression")
        return node

    def _expression(self) -> Node:
        """Parse ``term (("+"|"-") term)*``."""
        node = self._term()
        while (token := self._peek()) is not None and token.text in {"+", "-"}:
            self._position += 1
            node = Binary(token.text, node, self._term())
        return node

    def _term(self) -> Node:
        """Parse ``unary (("*"|"/") unary)*``."""
        node = self._unary()
        while (token := self._peek()) is not None and token.text in {"*", "/"}:
            self._position += 1
            node = Binary(token.text, node, self._unary())
        return node

    def _unary(self) -> Node:
        """Parse leading ``"+"``/``"-"`` signs then a primary; a unary plus leaves its operand unchanged."""
        signs = 0
        negations = 0
        while True:
            if self._accept("+"):
                signs += 1
            elif self._accept("-"):
                signs += 1
                negations += 1
            else:
                break
            if signs > MAX_NESTING:
                raise UnsupportedFormula(f"nested deeper than {MAX_NESTING} levels")
        node = self._primary()
        for _ in range(negations):
            node = Unary(node)
        return node

    def _primary(self) -> Node:
        """Parse a number, a reference, a function call or a parenthesized expression."""
        token = self._take()
        if token is None:
            raise UnsupportedFormula("the formula ends where a value is expected")
        if token.kind == "number":
            return Number(Decimal(token.text))
        if token.kind == "ref":
            return Ref(_normalize_ref(token.text))
        if token.kind == "name":
            return self._call(token)
        if token.text == "(":
            self._enter_nesting()
            node = self._expression()
            self._expect(")")
            self._depth -= 1
            return node
        if token.text == ":":
            raise UnsupportedFormula("a range is only allowed inside a function argument")
        raise UnsupportedFormula(f"unexpected {token.text!r} where a value is expected")

    def _call(self, token: _Token) -> Node:
        """Parse ``NAME "(" arguments ")"``, refusing names other than MIN, MAX and SUM."""
        name = token.text.upper()
        if name not in _FUNCTIONS:
            raise UnsupportedFormula(f"function {token.text!r} is not supported")
        if not self._accept("("):
            raise UnsupportedFormula(f"function {token.text!r} must be called with arguments")
        self._enter_nesting()
        args = self._arguments()
        self._expect(")")
        self._depth -= 1
        return Call(name, tuple(args))

    def _arguments(self) -> list[Node]:
        """Parse a possibly empty comma-separated argument list."""
        if self._peek_text(")"):
            return []
        args = [self._argument()]
        while self._accept(","):
            args.append(self._argument())
        return args

    def _argument(self) -> Node:
        """Parse one argument, expanding a ``REF ":" REF`` range into its cells."""
        first = self._peek()
        after = self._peek(1)
        if first is not None and first.kind == "ref" and after is not None and after.text == ":":
            self._position += 2
            end = self._take()
            if end is None or end.kind != "ref":
                raise UnsupportedFormula("a range needs a cell reference after ':'")
            return _range(first.text, end.text)
        return self._expression()

    def _enter_nesting(self) -> None:
        """Count one level of parentheses or call arguments, refusing more than Excel accepts."""
        self._depth += 1
        if self._depth > MAX_NESTING:
            raise UnsupportedFormula(f"nested deeper than {MAX_NESTING} levels")

    def _peek(self, offset: int = 0) -> _Token | None:
        """Return the token ``offset`` places ahead, or ``None`` past the end."""
        index = self._position + offset
        return self._tokens[index] if index < len(self._tokens) else None

    def _peek_text(self, text: str) -> bool:
        """Return whether the next token is ``text``."""
        token = self._peek()
        return token is not None and token.text == text

    def _take(self) -> _Token | None:
        """Consume and return the next token, or ``None`` past the end."""
        token = self._peek()
        if token is not None:
            self._position += 1
        return token

    def _accept(self, text: str) -> bool:
        """Consume the next token when it is ``text`` and return whether it was."""
        if not self._peek_text(text):
            return False
        self._position += 1
        return True

    def _expect(self, text: str) -> None:
        """Consume the next token, refusing anything but ``text``."""
        token = self._take()
        if token is not None and token.text == text:
            return
        if token is not None and token.text == ":":
            raise UnsupportedFormula("a range is only allowed inside a function argument")
        found = "the end of the formula" if token is None else repr(token.text)
        raise UnsupportedFormula(f"expected {text!r} but found {found}")


def _normalize_ref(text: str) -> str:
    """Return a cell reference without ``$`` and with an upper-case column."""
    return text.replace("$", "").upper()


def _range(start: str, end: str) -> Range:
    """Return the rectangle between two references, capped at 1000 cells."""
    start_cell = _normalize_ref(start)
    end_cell = _normalize_ref(end)
    start_column, start_row = _split_cell(start_cell)
    end_column, end_row = _split_cell(end_cell)
    columns = sorted((start_column, end_column))
    rows = sorted((start_row, end_row))
    _check_range_bounds(start_cell, end_cell, columns)
    count = (columns[1] - columns[0] + 1) * (rows[1] - rows[0] + 1)
    if count > _MAX_RANGE_CELLS:
        raise UnsupportedFormula(
            f"range {start_cell}:{end_cell} covers {count} cells; at most {_MAX_RANGE_CELLS} are supported"
        )
    cells = tuple(
        f"{_column_name(column)}{row}"
        for row in range(rows[0], rows[1] + 1)
        for column in range(columns[0], columns[1] + 1)
    )
    return Range(cells)


def _check_range_bounds(start: str, end: str, columns: Sequence[int]) -> None:
    """Refuse a range whose columns or rows Excel does not have."""
    if columns[0] < 1 or columns[1] > _MAX_COLUMN:
        raise UnsupportedFormula(f"range {start}:{end} is outside A:XFD")
    _, start_row = _split_cell(start)
    _, end_row = _split_cell(end)
    if start_row < 1 or end_row < 1:
        raise UnsupportedFormula(f"range {start}:{end} has a row below 1")


def _split_cell(cell: str) -> tuple[int, int]:
    """Return the ``(column, row)`` numbers of a normalized reference."""
    letters = cell.rstrip("0123456789")
    return _column_index(letters), int(cell[len(letters) :])


def _column_index(letters: str) -> int:
    """Return the 1-based column number of ``letters`` (A is 1)."""
    index = 0
    for character in letters:
        index = index * 26 + ord(character) - ord("A") + 1
    return index


def _column_name(index: int) -> str:
    """Return the column letters of a 1-based column number."""
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(ord("A") + remainder) + name
    return name
