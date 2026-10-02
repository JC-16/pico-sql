"""D1 in-memory execution engine (temporary implementation).

Day-1 goal: get SQL running end to end (parse -> execute -> result). Data
lives in plain Python lists and dies with the process -- deliberately so.
Day 2 replaces this with a slotted-page storage engine + LRU buffer pool,
and Day 4 adds WAL crash recovery. The commit history keeps this evolution
visible on purpose: a database is built layer by layer, not all at once.

Semantics notes (all documented in README "Known Limitations" too):
* v1 supports a single unquoted-identifier world: tables/columns are IDENTs.
* NULL comparisons return NULL (SQL three-valued logic); WHERE treats
  NULL as "not matched".
* Integer / integer division truncates toward zero, SQL-style.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from . import ast
from .lexer import SqlError  # noqa: F401  (re-exported for convenience)
from .parser import parse


class EngineError(Exception):
    """A runtime error (unknown table, type violation, constraint violation...)."""


# ------------------------------------------------------------------- results


@dataclass
class QueryResult:
    """Result of a SELECT: column names + row tuples."""

    columns: list
    rows: list


@dataclass
class ExecuteResult:
    """Result of a DDL/DML statement."""

    message: str
    affected: int = 0


# ---------------------------------------------------------------------- table


class Column:
    def __init__(self, defn: ast.ColumnDef):
        self.name = defn.name
        self.type = defn.type
        self.varchar_len = defn.varchar_len
        self.is_primary = defn.is_primary


class Table:
    """An in-memory table: schema + rows + a dict-based primary key index.

    The pk_index maps primary-key value -> row object. Day 3 replaces this
    dict with a real B+ tree that also supports range scans.
    """

    def __init__(self, stmt: ast.CreateTable):
        self.name = stmt.table
        self.columns = [Column(c) for c in stmt.columns]
        self.rows: list[list] = []
        pks = [c for c in self.columns if c.is_primary]
        self.pk: Optional[Column] = pks[0] if pks else None
        self.pk_index: dict = {}

    def column_names(self) -> list:
        return [c.name for c in self.columns]

    def col_pos(self, name: str) -> int:
        for i, c in enumerate(self.columns):
            if c.name == name:
                return i
        raise EngineError(f"unknown column {name!r} in table {self.name!r}")

    def _coerce(self, col: Column, value: Any) -> Any:
        """Validate and normalize a value against the column type."""
        if value is None:
            return None  # v1 has no NOT NULL constraint; NULL is always allowed
        if col.type == "INT":
            if isinstance(value, bool) or not isinstance(value, int):
                raise EngineError(
                    f"column {col.name!r} expects INT, got {type(value).__name__}"
                )
        elif col.type == "FLOAT":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EngineError(
                    f"column {col.name!r} expects FLOAT, got {type(value).__name__}"
                )
            return float(value)
        elif col.type == "VARCHAR":
            if not isinstance(value, str):
                raise EngineError(
                    f"column {col.name!r} expects VARCHAR, got {type(value).__name__}"
                )
            if col.varchar_len is not None and len(value) > col.varchar_len:
                raise EngineError(
                    f"value too long for {col.name!r} VARCHAR({col.varchar_len})"
                )
        elif col.type == "BOOL":
            if not isinstance(value, bool):
                raise EngineError(
                    f"column {col.name!r} expects BOOL, got {type(value).__name__}"
                )
        return value


# -------------------------------------------------------------- expressions


def eval_expr(expr: Any, row: Optional[list], table: Optional[Table]) -> Any:
    """Evaluate an expression AST node against a row.

    ``row is None`` means "no row context" (INSERT VALUES), where column
    references are invalid.
    """
    if isinstance(expr, ast.Literal):
        return expr.value

    if isinstance(expr, ast.ColumnRef):
        if row is None or table is None:
            raise EngineError(
                f"column reference {expr.name!r} is not allowed here (VALUES clause)"
            )
        return row[table.col_pos(expr.name)]

    if isinstance(expr, ast.UnaryOp):
        value = eval_expr(expr.operand, row, table)
        if expr.op == "NOT":
            return None if value is None else not bool(value)
        # NEG
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EngineError("unary '-' requires a numeric operand")
        return -value

    if isinstance(expr, ast.BinaryOp):
        op = expr.op
        if op in ("AND", "OR"):
            return _eval_logical(op, expr, row, table)
        left = eval_expr(expr.left, row, table)
        right = eval_expr(expr.right, row, table)
        if op in ("+", "-", "*", "/", "%"):
            return _eval_arith(op, left, right)
        return _eval_compare(op, left, right)

    raise EngineError(f"cannot evaluate node {type(expr).__name__}")


def _eval_logical(op: str, expr: ast.BinaryOp, row, table) -> Any:
    """SQL three-valued logic with short-circuit evaluation.

    AND: False dominates; otherwise None if any side is NULL.
    OR : True dominates;  otherwise None if any side is NULL.
    """
    left = eval_expr(expr.left, row, table)
    if op == "AND" and left is not None and not left:
        return False
    if op == "OR" and left is not None and left:
        return True
    right = eval_expr(expr.right, row, table)
    if right is not None and not right and op == "AND":
        return False
    if right is not None and right and op == "OR":
        return True
    if left is None or right is None:
        return None
    return bool(left) and bool(right) if op == "AND" else bool(left) or bool(right)


def _eval_arith(op: str, left: Any, right: Any) -> Any:
    if left is None or right is None:
        return None
    for v in (left, right):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EngineError(f"arithmetic requires numbers, got {type(v).__name__}")
    if op == "+":
        return left + right
    if op == "-":
        return left - right
    if op == "*":
        return left * right
    if op == "/":
        if right == 0:
            raise EngineError("division by zero")
        if isinstance(left, int) and isinstance(right, int):
            return int(left / right)  # SQL-style truncation, not Python floor
        return left / right
    # op == "%"
    if right == 0:
        raise EngineError("division by zero")
    return left % right  # Python semantics for negatives; see STUDY.md


def _eval_compare(op: str, left: Any, right: Any) -> Any:
    if left is None or right is None:
        return None
    if isinstance(left, bool) != isinstance(right, bool):
        raise EngineError("cannot compare BOOL with a number")
    if isinstance(left, str) != isinstance(right, str):
        raise EngineError(
            f"cannot compare {type(left).__name__} with {type(right).__name__}"
        )
    if op == "=":
        return left == right
    if op == "!=":
        return left != right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == ">":
        return left > right
    return left >= right  # ">="


def _where_pass(value: Any) -> bool:
    """WHERE keeps a row only when the predicate is truthy and not NULL."""
    return value is not None and bool(value)


def _order_key(value: Any) -> tuple:
    """NULL sorts first ascending / last descending (SQLite behavior)."""
    if value is None:
        return (0, 0)
    return (1, value)


# ------------------------------------------------------------------ executor


def _execute_create(db: "Database", stmt: ast.CreateTable) -> ExecuteResult:
    if stmt.table in db.tables:
        raise EngineError(f"table {stmt.table!r} already exists")
    db.tables[stmt.table] = Table(stmt)
    return ExecuteResult(f"table {stmt.table!r} created", affected=0)


def _execute_drop(db: "Database", stmt: ast.DropTable) -> ExecuteResult:
    if stmt.table not in db.tables:
        raise EngineError(f"no such table: {stmt.table!r}")
    del db.tables[stmt.table]
    return ExecuteResult(f"table {stmt.table!r} dropped", affected=0)


def _execute_insert(db: "Database", stmt: ast.Insert) -> ExecuteResult:
    table = db._require_table(stmt.table)
    ncols = len(table.columns)
    inserted = 0
    for row_exprs in stmt.rows:
        if stmt.columns is None:
            if len(row_exprs) != ncols:
                raise EngineError(
                    f"table {table.name!r} has {ncols} columns, "
                    f"got {len(row_exprs)} values"
                )
            targets = table.columns
        else:
            if len(row_exprs) != len(stmt.columns):
                raise EngineError(
                    f"{len(stmt.columns)} columns specified, "
                    f"got {len(row_exprs)} values"
                )
            targets = [table.columns[table.col_pos(c)] for c in stmt.columns]
        values: list = [None] * ncols
        for col, expr in zip(targets, row_exprs):
            values[table.col_pos(col.name)] = table._coerce(
                col, eval_expr(expr, None, table)
            )
        if table.pk is not None:
            pk_pos = table.col_pos(table.pk.name)
            pk_value = values[pk_pos]
            if pk_value is not None and pk_value in table.pk_index:
                raise EngineError(
                    f"duplicate primary key {pk_value!r} in table {table.name!r}"
                )
        table.rows.append(values)
        if table.pk is not None and values[pk_pos] is not None:
            table.pk_index[values[pk_pos]] = values
        inserted += 1
    return ExecuteResult(f"{inserted} row(s) inserted", affected=inserted)


def _execute_select(db: "Database", stmt: ast.Select) -> QueryResult:
    table = db._require_table(stmt.table)
    rows = table.rows
    if stmt.where is not None:
        rows = [r for r in rows if _where_pass(eval_expr(stmt.where, r, table))]
    if stmt.order_by is not None:
        col, desc = stmt.order_by
        pos = table.col_pos(col)
        rows = sorted(rows, key=lambda r: _order_key(r[pos]), reverse=desc)
    if stmt.columns == ["*"]:
        out_cols = table.column_names()
        out_rows = [list(r) for r in rows]
    else:
        out_cols = list(stmt.columns)
        positions = [table.col_pos(c) for c in stmt.columns]
        out_rows = [[r[i] for i in positions] for r in rows]
    if stmt.limit is not None:
        out_rows = out_rows[: stmt.limit]
    return QueryResult(out_cols, out_rows)


def _execute_update(db: "Database", stmt: ast.Update) -> ExecuteResult:
    table = db._require_table(stmt.table)
    positions: dict = {}
    for col, expr in stmt.assignments:
        pos = table.col_pos(col)
        if pos in positions:
            raise EngineError(f"duplicate column {col!r} in SET clause")
        positions[pos] = expr
    pk_pos = table.col_pos(table.pk.name) if table.pk is not None else None

    # Validate every row first, then commit -- a tiny nod to atomicity that
    # Day 4's WAL will make real.
    matched = [
        i
        for i, r in enumerate(table.rows)
        if stmt.where is None or _where_pass(eval_expr(stmt.where, r, table))
    ]
    updates = []
    for i in matched:
        row = table.rows[i]
        new_row = list(row)
        for pos, expr in positions.items():
            new_row[pos] = table._coerce(table.columns[pos], eval_expr(expr, row, table))
        if pk_pos is not None:
            old_value, new_value = row[pk_pos], new_row[pk_pos]
            if old_value != new_value:
                if (
                    new_value is not None
                    and new_value in table.pk_index
                    and table.pk_index[new_value] is not row
                ):
                    raise EngineError(
                        f"duplicate primary key {new_value!r} in table {table.name!r}"
                    )
        updates.append((i, row, new_row))

    for i, row, new_row in updates:
        table.rows[i] = new_row
        if pk_pos is not None:
            old_value, new_value = row[pk_pos], new_row[pk_pos]
            if old_value != new_value:
                if old_value is not None:
                    table.pk_index.pop(old_value, None)
                if new_value is not None:
                    table.pk_index[new_value] = new_row
    return ExecuteResult(f"{len(updates)} row(s) updated", affected=len(updates))


def _execute_delete(db: "Database", stmt: ast.Delete) -> ExecuteResult:
    table = db._require_table(stmt.table)
    pk_pos = table.col_pos(table.pk.name) if table.pk is not None else None
    kept: list = []
    deleted = 0
    for row in table.rows:
        matched = stmt.where is None or _where_pass(eval_expr(stmt.where, row, table))
        if matched:
            deleted += 1
            if pk_pos is not None and row[pk_pos] is not None:
                table.pk_index.pop(row[pk_pos], None)
        else:
            kept.append(row)
    table.rows = kept
    return ExecuteResult(f"{deleted} row(s) deleted", affected=deleted)


_DISPATCH = {
    ast.CreateTable: _execute_create,
    ast.DropTable: _execute_drop,
    ast.Insert: _execute_insert,
    ast.Select: _execute_select,
    ast.Update: _execute_update,
    ast.Delete: _execute_delete,
}


# ------------------------------------------------------------------- database


class Database:
    """A collection of in-memory tables. Day 2 adds a file-backed catalog."""

    def __init__(self):
        self.tables: dict = {}

    def _require_table(self, name: str) -> Table:
        table = self.tables.get(name)
        if table is None:
            raise EngineError(f"no such table: {name!r}")
        return table

    def execute(self, stmt):
        try:
            return _DISPATCH[type(stmt)](self, stmt)
        except RecursionError:
            raise EngineError("expression too deeply nested") from None

    def execute_sql(self, text: str) -> list:
        """Parse and run a whole script; returns one result per statement."""
        return [self.execute(stmt) for stmt in parse(text)]
