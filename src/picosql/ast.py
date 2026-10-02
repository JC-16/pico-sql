"""AST node definitions.

The parser consumes tokens and produces these nodes; the engine walks them.
Nodes are pure data (frozen dataclasses) -- no behavior lives here, which
keeps a clean separation between "what the SQL says" and "how to run it".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------- expressions


@dataclass(frozen=True)
class Literal:
    """A constant: int / float / str / bool / None."""

    value: Any


@dataclass(frozen=True)
class ColumnRef:
    """A reference to a column by name, e.g. ``score``."""

    name: str


@dataclass(frozen=True)
class BinaryOp:
    """A binary operation, e.g. ``a + b``, ``x = 1``, ``p AND q``."""

    op: str  # AND OR = != < <= > >= + - * / %
    left: Any
    right: Any


@dataclass(frozen=True)
class UnaryOp:
    """``NOT expr`` or unary minus ``-expr`` (op == 'NEG')."""

    op: str
    operand: Any


# ------------------------------------------------------------------ statements


@dataclass(frozen=True)
class ColumnDef:
    """One column inside CREATE TABLE."""

    name: str
    type: str  # INT / FLOAT / VARCHAR / BOOL
    varchar_len: Optional[int]
    is_primary: bool


@dataclass(frozen=True)
class CreateTable:
    table: str
    columns: list  # list[ColumnDef]


@dataclass(frozen=True)
class DropTable:
    table: str


@dataclass(frozen=True)
class Insert:
    table: str
    columns: Optional[list]  # None means "all columns, in definition order"
    rows: list  # list[list[expr]] -- one expression list per row


@dataclass(frozen=True)
class Select:
    table: str
    columns: list  # ["*"] or a list of column names
    where: Optional[Any]
    order_by: Optional[tuple]  # (column_name, descending: bool)
    limit: Optional[int]


@dataclass(frozen=True)
class Update:
    table: str
    assignments: list  # list[(column_name, expr)]
    where: Optional[Any]


@dataclass(frozen=True)
class Delete:
    table: str
    where: Optional[Any]
