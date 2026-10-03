import pytest

from picosql.storage.bufferpool import BufferPool, MemoryPageFile
from picosql.storage.heap import HeapTable
from picosql.storage.pages import PAGE_SIZE, PageError, new_page


class FakeColumn:
    def __init__(self, ctype, varchar_len=None):
        self.type = ctype
        self.varchar_len = varchar_len


def make_store(capacity=8):
    columns = [FakeColumn("INT"), FakeColumn("VARCHAR")]
    return HeapTable(BufferPool(MemoryPageFile(), capacity=capacity), [], columns)


def test_rows_span_multiple_pages():
    store = make_store()
    for i in range(120):
        store.insert([i, f"name-{i:060d}"])  # ~72 bytes/row -> ~53 rows/page
    assert len(store.page_ids) >= 2
    rows = [row for _, row in store.scan()]
    assert [r[0] for r in rows] == list(range(120))


def test_scan_skips_deleted_rows():
    store = make_store()
    rids = [store.insert([i, f"n{i}"]) for i in range(10)]
    store.delete(rids[3])
    store.delete(rids[7])
    rows = [row[0] for _, row in store.scan()]
    assert rows == [0, 1, 2, 4, 5, 6, 8, 9]


def test_update_in_place_keeps_row_id():
    store = make_store()
    rid = store.insert([1, "abcdef"])
    new_rid = store.update(rid, [1, "xyz"])  # shorter -> in place
    assert new_rid == rid
    rows = [row for _, row in store.scan()]
    assert rows == [[1, "xyz"]]


def test_update_longer_record_moves_row():
    store = make_store()
    rid = store.insert([1, "abc"])
    new_rid = store.update(rid, [1, "much longer string than before"])
    # The old bytes cannot hold the new record, so update() tombstones and
    # re-inserts. The returned row_id may even equal the old one when the
    # tombstoned slot is reused immediately -- the contract is only that it
    # points at exactly the new record, and nothing else remains.
    assert new_rid[0] == rid[0]  # same page, the reused tombstone slot
    rows = [row for _, row in store.scan()]
    assert rows == [[1, "much longer string than before"]]


def test_deleted_slot_cannot_be_updated():
    store = make_store()
    rid = store.insert([1, "a"])
    store.delete(rid)
    with pytest.raises(PageError, match="deleted"):
        store.update(rid, [1, "b"])


def test_row_too_large_for_any_page_rejected():
    store = make_store()
    huge = "x" * PAGE_SIZE  # can never fit in a 4 KB page
    with pytest.raises(PageError, match="never fit"):
        store.insert([1, huge])


def test_compaction_reclaims_fragmented_page():
    """Fill one page to ~full, punch holes, then insert rows that only fit
    after compaction. The table must never allocate a second page."""
    store = make_store()
    # INT row + VARCHAR(40 'n's): record = 9 + 43 = 52 bytes, slot 4 -> 56 B/row
    # 72 rows * 56 = 4032 of 4072 usable bytes: page is effectively full
    rids = [store.insert([i, "n" * 40]) for i in range(72)]
    assert len(store.page_ids) == 1
    for rid in rids[:20]:
        store.delete(rid)
    # 80-char names need 92-byte records; only compaction can make room
    for i in range(100, 110):
        store.insert([i, "x" * 80])
    assert len(store.page_ids) == 1  # compaction did its job
    rows = [row for _, row in store.scan()]
    assert len(rows) == 72 - 20 + 10
    assert [r[0] for r in rows if r[0] >= 100] == list(range(100, 110))


def test_alloc_hook_can_recycle_pages():
    store = make_store()
    recycled = store.pool.page_file.alloc_page()
    page = store.pool.get(recycled)
    page[:] = bytes(new_page(recycled))  # allocator contract: hand out INITIALIZED pages
    store.pool.mark_dirty(recycled)
    columns = [FakeColumn("INT"), FakeColumn("VARCHAR")]
    store2 = HeapTable(store.pool, [], columns, alloc_hook=lambda: recycled)
    rid = store2.insert([1, "reused"])
    assert rid[0] == recycled
    rows = [row for _, row in store2.scan()]
    assert rows == [[1, "reused"]]
