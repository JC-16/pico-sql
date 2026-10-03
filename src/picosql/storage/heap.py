"""Heap table: a table's rows spread across data pages, via the buffer pool.

row_id = (page_id, slot_no). Slot numbers are stable across compaction, but
``update()`` may move a row that no longer fits its old slot -- in that case
it tombstones the old slot and re-inserts, returning a NEW row_id. Callers
must adopt the returned row_id (the executor's primary-key index does).
"""

from __future__ import annotations

from typing import Callable, Iterator, Optional

from .bufferpool import BufferPool
from .pages import (
    HEADER_SIZE,
    PAGE_SIZE,
    PageError,
    SLOT_SIZE,
    compact,
    delete_record,
    free_space,
    insert_record,
    iterate_live,
    new_page,
    num_slots,
    update_record_inplace,
)
from .record import decode_row, encode_row


class HeapTable:
    def __init__(
        self,
        pool: BufferPool,
        page_ids: list,
        columns: list,
        alloc_hook: Optional[Callable[[], int]] = None,
    ):
        self.pool = pool
        self.page_ids: list = list(page_ids)
        self.columns = columns
        # New pages come from the database's allocator, which recycles pages
        # freed by DROP TABLE before extending the file. The allocator
        # contract: it returns a page_id whose page is INITIALIZED (valid
        # header). The default allocator does that initialization itself.
        self._alloc: Callable[[], int] = alloc_hook or self._default_alloc
        self._free_cache: dict = {}

    def _default_alloc(self) -> int:
        page_id = self.pool.page_file.alloc_page()
        page = self.pool.get(page_id)
        page[:] = bytes(new_page(page_id))
        self.pool.mark_dirty(page_id)
        return page_id

    # ------------------------------------------------------------------ reads

    def scan(self) -> Iterator:
        """Yield (row_id, row) for every live record, page by page."""
        for page_id in list(self.page_ids):
            page = self.pool.get(page_id)
            for slot_no, record in iterate_live(page):
                yield (page_id, slot_no), decode_row(self.columns, record)

    # ----------------------------------------------------------------- writes

    def insert(self, row: list) -> tuple:
        record = encode_row(self.columns, row)
        if len(record) > PAGE_SIZE - HEADER_SIZE - SLOT_SIZE:
            raise PageError(
                f"row needs {len(record)} bytes and can never fit in one page"
            )
        # first fit: reuse existing space (compacting fragmented pages) before
        # asking for a fresh page
        for page_id in list(self.page_ids):
            if self._ensure_room(page_id, len(record)):
                page = self.pool.get(page_id)
                slot_no = insert_record(page, record)
                self.pool.mark_dirty(page_id)
                self._free_cache[page_id] = free_space(page)
                return (page_id, slot_no)
        page_id = self._alloc()
        self.page_ids.append(page_id)
        page = self.pool.get(page_id)
        slot_no = insert_record(page, record)
        self.pool.mark_dirty(page_id)
        self._free_cache[page_id] = free_space(page)
        return (page_id, slot_no)

    def delete(self, row_id: tuple) -> None:
        page_id, slot_no = row_id
        page = self.pool.get(page_id)
        delete_record(page, slot_no)
        self.pool.mark_dirty(page_id)
        self._free_cache[page_id] = free_space(page)

    def update(self, row_id: tuple, row: list) -> tuple:
        """Overwrite in place if the new record fits the old slot's bytes;
        otherwise tombstone + re-insert. Returns the (possibly new) row_id."""
        page_id, slot_no = row_id
        record = encode_row(self.columns, row)
        if len(record) > PAGE_SIZE - HEADER_SIZE - SLOT_SIZE:
            raise PageError(
                f"row needs {len(record)} bytes and can never fit in one page"
            )
        page = self.pool.get(page_id)
        if update_record_inplace(page, slot_no, record):
            self.pool.mark_dirty(page_id)
            self._free_cache[page_id] = free_space(page)
            return row_id
        self.delete(row_id)
        return self.insert(row)

    # ----------------------------------------------------------- free space

    def _free(self, page_id: int) -> int:
        if page_id not in self._free_cache:
            page = self.pool.get(page_id)
            self._free_cache[page_id] = free_space(page)
        return self._free_cache[page_id]

    def _ensure_room(self, page_id: int, need: int) -> bool:
        """Make sure the page has ``need`` bytes free if at all possible.

        A page with tombstones may fit the record after compaction even when
        its raw free space says otherwise.
        """
        if self._free(page_id) >= need:
            return True
        page = self.pool.get(page_id)
        live = sum(1 for _ in iterate_live(page))
        if live == num_slots(page):
            return False  # no tombstones, nothing to reclaim
        reclaimed = compact(page)
        self.pool.mark_dirty(page_id)
        self._free_cache[page_id] = free_space(page)
        return reclaimed > 0 and self._free(page_id) >= need
