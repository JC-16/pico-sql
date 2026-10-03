"""SQL engine on top of the persistent storage layer (Day 2).

``Database(path)`` opens or creates a file-backed database: page 0 holds the
catalog (schemas + page lists as JSON), data pages follow. ``Database()``
without a path runs against a MemoryPageFile -- same code path, no file.

Crash semantics, honestly: writes reach the disk when the buffer pool
flushes (on close, or when a dirty page is evicted). A process crash before
that can lose data. Day 4's WAL closes this gap with write-ahead logging.

Rows are identified by row_id = (page_id, slot_no). The executor never sees
pages or bytes -- it talks to Table, which talks to HeapTable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import ast
from .executor import (
    FilterOperator,
    IndexPointScanOperator,
    IndexRangeScanOperator,
    LimitOperator,
    Operator,
    ProjectOperator,
    SeqScanOperator,
    SortOperator,
)
from .lexer import SqlError  # noqa: F401  (re-exported for convenience)
from .parser import parse
from .storage.btree import BPlusTree, BTreeError
from .storage.bufferpool import BufferPool, FilePageFile, MemoryPageFile
from .storage.catalog import load_catalog, save_catalog
from .storage.heap import HeapTable
from .storage.pages import (
    HEADER_SIZE,
    PAGE_SIZE,
    SLOT_SIZE,
    PageError,
    new_page,
)
from .storage.record import encode_row
from .storage.wal import WalError, WriteAheadLog

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


class EngineError(Exception):
    """A runtime error: unknown table, type violation, constraint violation."""


# ------------------------------------------------------------------- results


@dataclass
class QueryResult:
    """Result of a SELECT: column names + row values."""

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

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "type": self.type,
            "varchar_len": self.varchar_len,
            "primary": self.is_primary,
        }

    @classmethod
    def from_dict(cls, obj: dict) -> "Column":
        defn = ast.ColumnDef(
            obj["name"], obj["type"], obj.get("varchar_len"), obj.get("primary", False)
        )
        return cls(defn)


class Table:
    """Schema + heap store + primary-key B+ tree (row_id values)."""

    def __init__(self, name: str, columns: list, store: HeapTable):
        self.name = name
        self.columns = columns
        self.store = store
        pks = [c for c in columns if c.is_primary]
        self.pk: Optional[Column] = pks[0] if pks else None
        # B+ tree: key = primary-key value, value = row_id. NULL keys are
        # never inserted (documented deviation: NULL primary keys are
        # unindexed, so duplicates of NULL cannot be detected).
        self.pk_index = BPlusTree()

    def rebuild_pk_index(self) -> None:
        self.pk_index = BPlusTree()
        if self.pk is None:
            return
        pos = self.col_pos(self.pk.name)
        for rid, row in self.store.scan():
            if row[pos] is not None:
                self.pk_index.insert(row[pos], rid)

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
            if not INT64_MIN <= value <= INT64_MAX:
                # the on-disk encoding is a signed 8-byte int; without this
                # check an out-of-range value would explode as a raw
                # struct.error deep inside the storage layer
                raise EngineError(
                    f"value {value} out of INT64 range for column {col.name!r}"
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

    def insert_row(self, row: list) -> tuple:
        rid = self.store.insert(row)
        if self.pk is not None:
            value = row[self.col_pos(self.pk.name)]
            if value is not None:
                self.pk_index.insert(value, rid)
        return rid

    def delete_row(self, rid: tuple, row: Optional[list] = None) -> None:
        if self.pk is not None:
            value = row[self.col_pos(self.pk.name)] if row is not None else None
            if value is None:
                # caller didn't supply the row; recover the pk from the record
                pos = self.col_pos(self.pk.name)
                for scan_rid, scan_row in self.store.scan():
                    if scan_rid == rid:
                        value = scan_row[pos]
                        break
            if value is not None:
                self.pk_index.delete(value)
        self.store.delete(rid)

    def update_row(self, rid: tuple, old_row: list, new_row: list) -> tuple:
        actual = self.store.update(rid, new_row)
        if self.pk is not None:
            pos = self.col_pos(self.pk.name)
            old_value, new_value = old_row[pos], new_row[pos]
            if old_value != new_value:
                if old_value is not None:
                    self.pk_index.delete(old_value)
                if new_value is not None:
                    self.pk_index.insert(new_value, actual)
        return actual


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


# ------------------------------------------------------------------- executor


def _execute_create(db: "Database", stmt: ast.CreateTable) -> ExecuteResult:
    if stmt.table in db.tables:
        raise EngineError(f"table {stmt.table!r} already exists")
    columns = [Column(c) for c in stmt.columns]
    store = HeapTable(db.pool, [], columns, alloc_hook=db._alloc_page)
    db.tables[stmt.table] = Table(stmt.table, columns, store)
    db._save_catalog()
    return ExecuteResult(f"table {stmt.table!r} created")


def _execute_drop(db: "Database", stmt: ast.DropTable) -> ExecuteResult:
    if stmt.table not in db.tables:
        raise EngineError(f"no such table: {stmt.table!r}")
    table = db.tables.pop(stmt.table)
    # recycle the table's pages for future tables; old bytes linger until
    # each page is reused -- same visibility tradeoff real engines make
    db.free_pages.extend(table.store.page_ids)
    db._save_catalog()
    return ExecuteResult(f"table {stmt.table!r} dropped")


def _execute_insert(db: "Database", stmt: ast.Insert) -> ExecuteResult:
    table = db._require_table(stmt.table)
    ncols = len(table.columns)
    pk_pos = table.col_pos(table.pk.name) if table.pk is not None else None

    # Phase 1 -- validate everything, write nothing. A statement that fails
    # anywhere (type, size, duplicate key) must leave the table untouched.
    staged: list = []
    for row_exprs in stmt.rows:
        if stmt.columns is None:
            if len(row_exprs) != ncols:
                raise EngineError(
                    f"table {table.name!r} has {ncols} columns, got {len(row_exprs)} values"
                )
            targets = table.columns
        else:
            if len(row_exprs) != len(stmt.columns):
                raise EngineError(
                    f"{len(stmt.columns)} columns specified, got {len(row_exprs)} values"
                )
            targets = [table.columns[table.col_pos(c)] for c in stmt.columns]
        values: list = [None] * ncols
        for col, expr in zip(targets, row_exprs):
            values[table.col_pos(col.name)] = table._coerce(
                col, eval_expr(expr, None, table)
            )
        encoded = encode_row(table.columns, values)
        if len(encoded) > PAGE_SIZE - HEADER_SIZE - SLOT_SIZE:
            # trial-encode passes the codec but the row can never live in a
            # page -- reject it in phase 1, BEFORE phase 2 starts writing
            raise EngineError(
                f"row needs {len(encoded)} bytes and cannot fit in one page"
            )
        staged.append(values)
    if pk_pos is not None:
        seen: set = set()
        for values in staged:
            pk_value = values[pk_pos]
            if pk_value is None:
                continue
            if pk_value in table.pk_index or pk_value in seen:
                raise EngineError(
                    f"duplicate primary key {pk_value!r} in table {table.name!r}"
                )
            seen.add(pk_value)

    # Phase 2 -- commit: nothing below can fail, by construction
    for values in staged:
        table.insert_row(values)
    return ExecuteResult(f"{len(staged)} row(s) inserted", affected=len(staged))


def _execute_select(db: "Database", stmt: ast.Select) -> QueryResult:
    table = db._require_table(stmt.table)

    # --- plan: can the WHERE clause resolve (partially) through the PK index?
    factors = _split_conjunction(stmt.where)
    plan, residual_factors = _extract_pk_plan(table, factors)
    db.last_scan_used_index = plan is not None
    if plan is not None:
        scan: Operator = plan(table)
        residual = _combine_conjunction(residual_factors)
    else:
        scan = SeqScanOperator(table)
        residual = stmt.where

    # --- pipeline: scan -> filter(residual) -> sort -> project -> limit
    op: Operator = scan
    if residual is not None:
        op = FilterOperator(op, lambda row: _where_pass(eval_expr(residual, row, table)))
    if stmt.order_by is not None:
        col, desc = stmt.order_by
        pos = table.col_pos(col)
        op = SortOperator(op, lambda row: _order_key(row[pos]), reverse=desc)
    if stmt.columns == ["*"]:
        out_cols = table.column_names()
    else:
        positions = [table.col_pos(c) for c in stmt.columns]
        out_cols = list(stmt.columns)
        op = ProjectOperator(op, positions)
    if stmt.limit is not None:
        op = LimitOperator(op, stmt.limit)

    return QueryResult(out_cols, op.drain())


def _split_conjunction(expr) -> list:
    """Flatten an AND-chain into a list of factors (WHERE -> [f1, f2, ...])."""
    if expr is None:
        return []
    if isinstance(expr, ast.BinaryOp) and expr.op == "AND":
        return _split_conjunction(expr.left) + _split_conjunction(expr.right)
    return [expr]


def _combine_conjunction(factors: list):
    if not factors:
        return None
    expr = factors[0]
    for factor in factors[1:]:
        expr = ast.BinaryOp("AND", expr, factor)
    return expr


_EXTRACTABLE_OPS = ("=", "<", "<=", ">", ">=")
_MIRROR_OP = {"<": ">", "<=": ">=", ">": "<", ">=": "<="}


def _tighten(bound_list: list, lower: bool) -> tuple:
    """Reduce (value, inclusive) pairs to the tightest single bound.

    lower=True  -> the maximum value wins; on ties, exclusivity wins.
    lower=False -> the minimum value wins; on ties, exclusivity wins.
    Returns (value, inclusive) or (None, True) for an empty list.
    """
    if not bound_list:
        return None, True
    values = [v for v, _ in bound_list]
    best = max(values) if lower else min(values)
    inclusive = all(inc for v, inc in bound_list if v == best)
    return best, inclusive

def _extract_pk_plan(table: "Table", factors: list):
    """Look for primary-key predicates a B+ tree can resolve.

    Returns (plan_fn | None, residual_factors). plan_fn(table) builds the
    index scan operator. Only conjunctive factors of the form
    ``pk OP literal`` (or the mirrored ``literal OP pk``) with OP in
    =, <, <=, >, >= qualify. ``!=``/``<>`` deliberately do NOT: a disequality
    is the union of two ranges, not a bound -- misreading it as an upper
    bound would silently drop every row above the literal.

    Multiple bounds are TIGHTENED, never overwritten: lo = max of lower
    bounds (exclusivity wins on ties), hi = min of upper bounds. A tightened
    range implies every individual range factor, so range factors are fully
    consumed by the scan. When an equality coexists with other pk factors,
    the point scan runs on the first equality and the remaining pk factors
    are demoted into the residual filter -- contradictory conjunctions
    (id = 2 AND id > 3, id = 5 AND id = 6) then correctly return nothing
    instead of whatever the first bound happened to match.
    """
    if table.pk is None:
        return None, factors
    pk_name = table.pk.name
    eqs: list = []
    lo_list: list = []
    hi_list: list = []
    residual: list = []

    for factor in factors:
        if isinstance(factor, ast.BinaryOp) and factor.op in _EXTRACTABLE_OPS:
            left, right = factor.left, factor.right
            if (
                isinstance(right, ast.Literal)
                and isinstance(left, ast.ColumnRef)
                and left.name == pk_name
            ):
                op, value = factor.op, right.value
            elif (
                isinstance(left, ast.Literal)
                and isinstance(right, ast.ColumnRef)
                and right.name == pk_name
            ):
                # "=" mirrors to itself; the dict only holds asymmetric ops
                op, value = _MIRROR_OP.get(factor.op, factor.op), left.value
            else:
                residual.append(factor)
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                residual.append(factor)  # NULL literal etc. -- useless as a bound
                continue
            if op == "=":
                eqs.append((value, factor))
            elif op in (">", ">="):
                lo_list.append((value, op == ">=", factor))
            else:
                hi_list.append((value, op == "<=", factor))
        else:
            residual.append(factor)

    # string bounds cannot be compared against numeric keys inside the tree
    all_values = [v for v, _ in eqs] + [v for v, _, _ in lo_list] + [
        v for v, _, _ in hi_list
    ]
    if all_values and any(
        isinstance(v, str) != isinstance(all_values[0], str) for v in all_values
    ):
        return None, factors

    if eqs:
        # point scan on the first equality; every other pk factor (further
        # equalities, ranges) is demoted into the residual so contradictory
        # conjunctions still filter correctly
        demoted = [f for _, f in eqs[1:]]
        demoted += [f for _, _, f in lo_list]
        demoted += [f for _, _, f in hi_list]
        return lambda t: IndexPointScanOperator(t, eqs[0][0]), residual + demoted

    if lo_list or hi_list:
        lo, lo_inc = _tighten([(v, inc) for v, inc, _ in lo_list], lower=True)
        hi, hi_inc = _tighten([(v, inc) for v, inc, _ in hi_list], lower=False)
        # the tightened range implies every individual range factor, so they
        # are all consumed; only non-pk factors stay in the residual
        return (
            lambda t: IndexRangeScanOperator(t, lo, lo_inc, hi, hi_inc),
            residual,
        )
    return None, factors


def _execute_update(db: "Database", stmt: ast.Update) -> ExecuteResult:
    table = db._require_table(stmt.table)
    positions: dict = {}
    for col, expr in stmt.assignments:
        pos = table.col_pos(col)
        if pos in positions:
            raise EngineError(f"duplicate column {col!r} in SET clause")
        positions[pos] = expr
    pk_pos = table.col_pos(table.pk.name) if table.pk is not None else None

    # Phase 1 -- validate + encode every row; nothing is written yet.
    # Statement-level all-or-nothing. Day 4's WAL extends this guarantee
    # from "no partial statement" to "no partial statement after a crash".
    matched = [
        (rid, row)
        for rid, row in table.store.scan()
        if stmt.where is None or _where_pass(eval_expr(stmt.where, row, table))
    ]
    updates = []
    seen_new_pks: set = set()
    for rid, row in matched:
        new_row = list(row)
        for pos, expr in positions.items():
            new_row[pos] = table._coerce(table.columns[pos], eval_expr(expr, row, table))
        encoded = encode_row(table.columns, new_row)
        if len(encoded) > PAGE_SIZE - HEADER_SIZE - SLOT_SIZE:
            raise EngineError(
                f"row needs {len(encoded)} bytes and cannot fit in one page"
            )
        if pk_pos is not None:
            old_value, new_value = row[pk_pos], new_row[pk_pos]
            if old_value != new_value and new_value is not None:
                existing = table.pk_index.search(new_value)
                if existing is not None and existing != rid:
                    raise EngineError(
                        f"duplicate primary key {new_value!r} in table {table.name!r}"
                    )
                if new_value in seen_new_pks:
                    raise EngineError(
                        f"duplicate primary key {new_value!r} in table {table.name!r}"
                    )
                seen_new_pks.add(new_value)
        updates.append((rid, row, new_row))

    for rid, old_row, new_row in updates:
        table.update_row(rid, old_row, new_row)
    return ExecuteResult(f"{len(updates)} row(s) updated", affected=len(updates))


def _execute_delete(db: "Database", stmt: ast.Delete) -> ExecuteResult:
    table = db._require_table(stmt.table)
    matched = [
        (rid, row)
        for rid, row in table.store.scan()
        if stmt.where is None or _where_pass(eval_expr(stmt.where, row, table))
    ]
    for rid, row in matched:
        table.delete_row(rid, row)
    return ExecuteResult(f"{len(matched)} row(s) deleted", affected=len(matched))


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
    """A collection of tables backed by the storage layer.

    ``Database()``          -> ephemeral, in-memory pages
    ``Database("db.pico")`` -> file-backed; survives close() + reopen
    """

    def __init__(self, path=None, buffer_capacity: int = 64):
        self.page_file = (
            MemoryPageFile() if path is None else FilePageFile(Path(path))
        )
        self.pool = BufferPool(self.page_file, buffer_capacity)
        self._closed = False
        self._poisoned = False
        self._commits_since_checkpoint = 0
        # File mode gets a WAL: anything that returns from execute() is
        # fsynced into the log before the caller sees success, so a crash
        # can never lose a committed statement. Memory mode is ephemeral by
        # definition -- nothing to recover, nothing to log.
        self._wal = None if path is None else WriteAheadLog(Path(str(path) + ".wal"))
        if self._wal is not None:
            self._recover_from_wal()
        try:
            self.catalog = load_catalog(self.pool)
        except PageError as exc:
            self.pool.close()
            self.page_file.close()
            raise EngineError(f"cannot open database: {exc}") from None
        self.free_pages: list = list(self.catalog.get("free_pages", []))
        self.tables: dict = {}
        for name, entry in self.catalog["tables"].items():
            columns = [Column.from_dict(c) for c in entry["columns"]]
            store = HeapTable(
                self.pool, entry["page_ids"], columns, alloc_hook=self._alloc_page
            )
            table = Table(name, columns, store)
            table.rebuild_pk_index()
            self.tables[name] = table
        self.last_scan_used_index = None  # set by every SELECT (planner output)

    def _recover_from_wal(self) -> int:
        """Replay committed records into the data file, then truncate the log.

        Returns the number of records applied (0 = the log was empty; nothing
        to recover).
        """
        applied = 0
        for _lsn, pages in self._wal.replay():
            for page_id, image in pages:
                while page_id >= self.page_file.num_pages:
                    self.page_file.alloc_page()
                self.page_file.write_page(page_id, image)
            applied += 1
        if applied:
            self.page_file.sync()
            self._wal.truncate()  # redo applied: the log is redundant again
        return applied

    def _wal_commit(self) -> None:
        """Autocommit: fsync a full-page-image record of every dirty page.

        The catalog is re-serialized FIRST (page_ids and the free list change
        on every allocation), then all dirty pages are snapshotted -- page 0
        among them, so the log always carries the catalog that matches the
        data pages. Snapshotting ALL dirty pages (not just this statement's)
        is correct: their current images are the last committed state, and
        redo replays in order, so the newest image always wins.
        """
        if self._wal is None:
            return
        self._save_catalog()
        dirty = sorted(self.pool.dirty_page_ids())
        if not dirty:
            return
        images = [(page_id, bytes(self.pool.get(page_id))) for page_id in dirty]
        try:
            self._wal.append_commit(images)
        except WalError as exc:
            # the statement applied in memory but is NOT durable -- the only
            # safe move is to fail the instance; reopening replays the last
            # committed state and discards this work
            self._poisoned = True
            raise EngineError(f"commit failed: {exc}") from None
        self._commits_since_checkpoint += 1
        if self._commits_since_checkpoint >= 32:
            self.checkpoint()

    def checkpoint(self) -> None:
        """Mid-session checkpoint: flush every dirty page to the data file,
        fsync, then truncate the WAL (redundant once pages are durable)."""
        if self._closed or self._poisoned:
            return
        self._flush_and_truncate()

    def _flush_and_truncate(self) -> None:
        self.pool.flush_all()
        self.page_file.sync()
        self._commits_since_checkpoint = 0
        if self._wal is not None:
            self._wal.truncate()

    def _alloc_page(self) -> int:
        """Allocate a page for table data: recycle dropped pages first.

        Both fresh and recycled pages are (re-)initialized with a valid
        header -- a recycled page still carries the dropped table's data
        until every byte of it happens to be overwritten. The catalog is
        NOT serialized here: _wal_commit serializes it after the caller has
        updated page_ids, so the logged image is always current.
        """
        recycled = bool(self.free_pages)
        page_id = self.free_pages.pop(0) if recycled else self.pool.page_file.alloc_page()
        page = self.pool.get(page_id)
        page[:] = bytes(new_page(page_id))
        self.pool.mark_dirty(page_id)
        return page_id

    def _save_catalog(self) -> None:
        self.catalog["tables"] = {
            name: {
                "columns": [c.to_dict() for c in table.columns],
                "page_ids": table.store.page_ids,
            }
            for name, table in self.tables.items()
        }
        self.catalog["free_pages"] = self.free_pages
        save_catalog(self.pool, self.catalog)

    def _require_table(self, name: str) -> Table:
        table = self.tables.get(name)
        if table is None:
            raise EngineError(f"no such table: {name!r}")
        return table

    def execute(self, stmt):
        if self._closed:
            raise EngineError("database is closed")
        if self._poisoned:
            raise EngineError(
                "database is in a failed state; close and reopen to recover "
                "the last committed state"
            )
        try:
            result = _DISPATCH[type(stmt)](self, stmt)
        except PageError as exc:
            # a storage failure mid-statement may have left partial changes
            # in the buffer pool: poison the instance so those uncommitted
            # pages are never flushed; reopening replays the WAL instead
            self._poisoned = True
            raise EngineError(str(exc)) from None
        except BTreeError as exc:
            self._poisoned = True
            raise EngineError(str(exc)) from None
        except RecursionError:
            self._poisoned = True
            raise EngineError("expression too deeply nested") from None
        self._wal_commit()
        return result

    def execute_sql(self, text: str) -> list:
        """Parse and run a whole script; returns one result per statement."""
        return [self.execute(stmt) for stmt in parse(text)]

    def close(self) -> None:
        """Checkpoint (flush + fsync + WAL truncate) and close. Idempotent.

        A poisoned instance discards its dirty pages instead of flushing
        them: uncommitted work must never reach the data file. Reopening
        replays the WAL to the last committed state.
        """
        if self._closed:
            return
        if self._poisoned:
            self._closed = True
            self.pool.discard()
            self.page_file.close()
            return
        self._save_catalog()  # capture the final catalog (page_ids, free list)
        self._flush_and_truncate()
        self._closed = True
        self.page_file.close()


__all__ = [
    "Database",
    "EngineError",
    "ExecuteResult",
    "QueryResult",
    "Table",
]
