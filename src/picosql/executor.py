"""Volcano-model operators: the classic iterator interface for query execution.

Every operator implements ``open() / next() / close()``. ``next()`` returns
one row (a list of values) or ``None`` when exhausted. Operators compose
into a pipeline: data is PULLED from the bottom (scan) upward, one row at a
time, which is why ``LimitOperator`` can stop the world after N rows without
reading the rest -- see the laziness test.

Honest note: SortOperator is BLOCKING (it drains its child before yielding),
as every real sort must be; real engines add external merge sort for inputs
bigger than memory. DML statements (INSERT/UPDATE/DELETE) stay batch-shaped
in v1; a full engine would also express them as operators.
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional


class Operator:
    def open(self) -> None:
        raise NotImplementedError

    def next(self) -> Optional[list]:
        """Return the next row, or None when exhausted."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def drain(self) -> list:
        """Convenience: open, collect everything, close."""
        self.open()
        try:
            rows = []
            while True:
                row = self.next()
                if row is None:
                    return rows
                rows.append(row)
        finally:
            self.close()


class SeqScanOperator(Operator):
    """Full table scan: every live row, page by page."""

    def __init__(self, table):
        self.table = table
        self._it: Optional[Iterator] = None

    def open(self) -> None:
        self._it = self.table.store.scan()

    def next(self) -> Optional[list]:
        pair = next(self._it, None)
        return None if pair is None else pair[1]

    def close(self) -> None:
        self._it = None


class IndexPointScanOperator(Operator):
    """Primary-key equality: one O(log n) tree descent + one page fetch."""

    def __init__(self, table, key):
        self.table = table
        self.key = key
        self._done = False

    def open(self) -> None:
        self._done = False

    def next(self) -> Optional[list]:
        if self._done:
            return None
        self._done = True
        rid = self.table.pk_index.search(self.key)
        return None if rid is None else self.table.store.fetch(rid)


class IndexRangeScanOperator(Operator):
    """Primary-key range: leaf-chain walk + one page fetch per row."""

    def __init__(self, table, lo, lo_inclusive, hi, hi_inclusive):
        self.table = table
        self.lo, self.lo_inclusive = lo, lo_inclusive
        self.hi, self.hi_inclusive = hi, hi_inclusive
        self._it: Optional[Iterator] = None

    def open(self) -> None:
        self._it = self.table.pk_index.range_scan(
            self.lo, self.lo_inclusive, self.hi, self.hi_inclusive
        )

    def next(self) -> Optional[list]:
        pair = next(self._it, None)
        return None if pair is None else self.table.store.fetch(pair[1])

    def close(self) -> None:
        self._it = None


class FilterOperator(Operator):
    """Predicates the index could not resolve (the residual)."""

    def __init__(self, child: Operator, predicate: Callable):
        self.child = child
        self.predicate = predicate

    def open(self) -> None:
        self.child.open()

    def next(self) -> Optional[list]:
        while True:
            row = self.child.next()
            if row is None:
                return None
            if self.predicate(row):
                return row

    def close(self) -> None:
        self.child.close()


class SortOperator(Operator):
    """BLOCKING: drains the child at open(), then yields in sorted order."""

    def __init__(self, child: Operator, key_fn: Callable, reverse: bool = False):
        self.child = child
        self.key_fn = key_fn
        self.reverse = reverse
        self._rows: Optional[list] = None
        self._pos = 0

    def open(self) -> None:
        self.child.open()
        rows = []
        while True:
            row = self.child.next()
            if row is None:
                break
            rows.append(row)
        rows.sort(key=self.key_fn, reverse=self.reverse)
        self._rows = rows
        self._pos = 0
        self.child.close()

    def next(self) -> Optional[list]:
        if self._rows is None:
            raise RuntimeError("open() must be called before next()")
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row


class ProjectOperator(Operator):
    """Column projection: full row -> selected positions."""

    def __init__(self, child: Operator, positions: list):
        self.child = child
        self.positions = positions

    def open(self) -> None:
        self.child.open()

    def next(self) -> Optional[list]:
        row = self.child.next()
        return None if row is None else [row[i] for i in self.positions]

    def close(self) -> None:
        self.child.close()


class LimitOperator(Operator):
    """Stops pulling from the child after N rows -- pure laziness."""

    def __init__(self, child: Operator, limit: int):
        self.child = child
        self.limit = limit
        self._taken = 0
        self._opened = False

    def open(self) -> None:
        self.child.open()
        self._opened = True

    def next(self) -> Optional[list]:
        if not self._opened:
            raise RuntimeError("open() must be called before next()")
        if self._taken >= self.limit:
            return None
        row = self.child.next()
        if row is None:
            return None
        self._taken += 1
        return row

    def close(self) -> None:
        self.child.close()


class CountingOperator(Operator):
    """Test instrument: counts how many rows were actually pulled."""

    def __init__(self, child: Operator):
        self.child = child
        self.pulled = 0

    def open(self) -> None:
        self.child.open()

    def next(self) -> Optional[list]:
        row = self.child.next()
        if row is not None:
            self.pulled += 1
        return row

    def close(self) -> None:
        self.child.close()
