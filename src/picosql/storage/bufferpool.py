"""Page files (memory & disk) + LRU buffer pool.

BufferPool is the database's own page cache: executors never touch the
page file directly. Reads go through ``get()`` (hits stay in memory,
misses are loaded from disk); writes mutate the returned buffer and call
``mark_dirty()``. Evicted dirty pages are written back first -- a page is
never lost while the pool is alive. Durability across crashes is Day 4's
(WAL) job; until then a crash before ``close()`` can lose dirty pages.

Hit/miss counters are exposed for the Day 4 benchmark.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from .pages import PAGE_SIZE, PageError, new_page


# ------------------------------------------------------------------ page files


class MemoryPageFile:
    """In-memory page store: same contract as FilePageFile, no persistence.

    Used for ephemeral databases and for tests -- it makes the page layer
    independently testable without touching the disk.
    """

    def __init__(self):
        self._pages: dict = {}

    @property
    def num_pages(self) -> int:
        return len(self._pages)

    def read_page(self, page_id: int) -> bytes:
        if not 0 <= page_id < self.num_pages:
            raise PageError(f"page {page_id} out of range (have {self.num_pages})")
        return bytes(self._pages[page_id])

    def write_page(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise PageError(f"write size is {len(data)}, expected {PAGE_SIZE}")
        if not 0 <= page_id < self.num_pages:
            raise PageError(f"page {page_id} out of range (have {self.num_pages})")
        self._pages[page_id] = bytearray(data)

    def alloc_page(self) -> int:
        page_id = self.num_pages
        self._pages[page_id] = bytearray(PAGE_SIZE)
        return page_id

    def sync(self) -> None:  # nothing to sync in memory
        pass

    def close(self) -> None:
        pass


class FilePageFile:
    """A disk-backed page file: pure sequence of PAGE_SIZE byte pages."""

    def __init__(self, path):
        self.path = Path(path)
        self._closed = False
        try:
            if not self.path.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.path.write_bytes(b"")
            self._file = open(self.path, "r+b")
            self._file.seek(0, os.SEEK_END)
            size = self._file.tell()
        except OSError as exc:
            # open() failures (permissions, locked file, bad path) must not
            # leak raw OSError through the storage boundary
            raise PageError(f"cannot open database file {self.path}: {exc}") from None
        if size % PAGE_SIZE != 0:
            self._file.close()
            raise PageError(
                f"{self.path} is truncated ({size} bytes, not a page multiple)"
            )
        self._num_pages = size // PAGE_SIZE

    @property
    def num_pages(self) -> int:
        return self._num_pages

    def _check_open(self) -> None:
        if self._closed:
            raise PageError("page file is closed")

    def read_page(self, page_id: int) -> bytes:
        self._check_open()
        if not 0 <= page_id < self._num_pages:
            raise PageError(f"page {page_id} out of range (have {self._num_pages})")
        self._file.seek(page_id * PAGE_SIZE)
        data = self._file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise PageError(f"short read on page {page_id}")
        return data

    def write_page(self, page_id: int, data: bytes) -> None:
        self._check_open()
        if len(data) != PAGE_SIZE:
            raise PageError(f"write size is {len(data)}, expected {PAGE_SIZE}")
        if not 0 <= page_id < self._num_pages:
            raise PageError(f"page {page_id} out of range (have {self._num_pages})")
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(data)

    def alloc_page(self) -> int:
        self._check_open()
        page_id = self._num_pages
        self._file.seek(page_id * PAGE_SIZE)
        self._file.write(bytes(PAGE_SIZE))
        self._num_pages += 1
        return page_id

    def sync(self) -> None:
        if self._closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._closed:
            return  # idempotent: double close must not crash
        self.sync()
        self._file.close()
        self._closed = True


# ----------------------------------------------------------------- buffer pool


class BufferPool:
    """Fixed-capacity LRU cache over a page file.

    * ``get(pid)`` returns a MUTABLE bytearray -- mutate it, then call
      ``mark_dirty(pid)``.
    * Eviction: the least recently used page is discarded; if dirty, it is
      written back first.
    * ``close()`` flushes every dirty page and fsyncs the file.
    """

    def __init__(self, page_file, capacity: int = 64):
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.page_file = page_file
        self.capacity = capacity
        self._pages: OrderedDict = OrderedDict()  # page_id -> bytearray, LRU order
        self._dirty: set = set()
        self.hits = 0
        self.misses = 0
        self._closed = False
        # WAL support: a monotonically increasing counter, bumped on every
        # mark_dirty, plus per-page "serial at last dirtied time". The engine
        # compares max(serial) with the serial it last logged to decide
        # whether anything needs a commit record -- the per-page-LSN idea
        # reduced to one global counter.
        self._serial = 0
        self._page_serial: dict = {}

    def _check_open(self) -> None:
        if self._closed:
            raise PageError("buffer pool is closed")

    def get(self, page_id: int) -> bytearray:
        self._check_open()
        page = self._pages.get(page_id)
        if page is not None:
            self.hits += 1
            self._pages.move_to_end(page_id)
            return page
        self.misses += 1
        if len(self._pages) >= self.capacity:
            self._evict_one()
        self._pages[page_id] = bytearray(self.page_file.read_page(page_id))
        return self._pages[page_id]

    def _evict_one(self) -> None:
        page_id, page = self._pages.popitem(last=False)  # least recently used
        if page_id in self._dirty:
            self.page_file.write_page(page_id, bytes(page))
            self._dirty.discard(page_id)
        self._page_serial.pop(page_id, None)  # durable: no logging needed

    def mark_dirty(self, page_id: int) -> None:
        self._check_open()
        if page_id not in self._pages:
            raise PageError(f"cannot mark non-resident page {page_id} dirty")
        self._dirty.add(page_id)
        self._serial += 1
        self._page_serial[page_id] = self._serial

    def flush(self, page_id: int) -> None:
        if page_id in self._pages and page_id in self._dirty:
            self.page_file.write_page(page_id, bytes(self._pages[page_id]))
            self._dirty.discard(page_id)
        self._page_serial.pop(page_id, None)  # durable: no logging needed

    def flush_all(self) -> None:
        for page_id in list(self._dirty):
            self.page_file.write_page(page_id, bytes(self._pages[page_id]))
        self._dirty.clear()
        self._page_serial.clear()

    def discard(self) -> None:
        """Drop every cached page and dirty flag WITHOUT writing anything.

        Used when an instance is poisoned: uncommitted pages must never
        reach the data file.
        """
        self._check_open()
        self._pages.clear()
        self._dirty.clear()
        self._page_serial.clear()

    def dirty_page_ids(self) -> frozenset:
        return frozenset(self._dirty)

    def max_dirty_serial(self) -> int:
        """Highest mark_dirty serial among currently dirty pages."""
        return max(self._page_serial.values(), default=0)

    def current_serial(self) -> int:
        return self._serial

    @property
    def dirty_count(self) -> int:
        return len(self._dirty)

    @property
    def size(self) -> int:
        return len(self._pages)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def close(self) -> None:
        if self._closed:
            return  # idempotent
        self._closed = True
        self.flush_all()
        self.page_file.sync()


__all__ = ["BufferPool", "FilePageFile", "MemoryPageFile", "new_page"]
