import pytest

from picosql.storage.bufferpool import BufferPool, FilePageFile, MemoryPageFile
from picosql.storage.pages import PageError, new_page


def test_memory_and_file_page_files_behave_identically(tmp_path):
    for pf in (MemoryPageFile(), FilePageFile(tmp_path / "db.pico")):
        p0 = pf.alloc_page()
        p1 = pf.alloc_page()
        assert (p0, p1) == (0, 1)
        assert pf.num_pages == 2
        pf.write_page(0, bytes(new_page(0)))
        pf.write_page(1, bytes(new_page(1)))
        assert pf.read_page(0) == bytes(new_page(0))
        assert pf.read_page(1) == bytes(new_page(1))
        with pytest.raises(PageError, match="out of range"):
            pf.read_page(5)
        with pytest.raises(PageError, match="expected"):
            pf.write_page(0, b"short")
        pf.close()


def test_buffer_pool_counts_hits_and_misses():
    pf = MemoryPageFile()
    p0 = pf.alloc_page()
    pool = BufferPool(pf, capacity=4)
    pool.get(p0)
    pool.get(p0)
    pool.get(p0)
    assert (pool.misses, pool.hits) == (1, 2)


def test_lru_eviction_order():
    pf = MemoryPageFile()
    ids = [pf.alloc_page() for _ in range(4)]
    pool = BufferPool(pf, capacity=2)
    pool.get(ids[0])
    pool.get(ids[1])
    pool.get(ids[0])  # touch 0 -> LRU order is [1, 0]
    pool.get(ids[2])  # miss; evicts 1 (least recently used)
    pool.get(ids[3])  # miss; evicts 0
    assert pool.size == 2
    assert (pool.hits, pool.misses) == (1, 4)
    pool.get(ids[1])  # 1 was evicted -> another miss (evicts 2)
    assert (pool.hits, pool.misses) == (1, 5)
    pool.get(ids[1])  # resident now -> hit
    assert (pool.hits, pool.misses) == (2, 5)


def test_dirty_page_written_back_on_eviction():
    pf = MemoryPageFile()
    pid = pf.alloc_page()
    pool = BufferPool(pf, capacity=1)
    page = pool.get(pid)
    page[24:28] = b"MARK"
    pool.mark_dirty(pid)
    assert pool.dirty_count == 1
    pool.get(pf.alloc_page())  # evicts the dirty page -> written to file
    assert pool.dirty_count == 0
    assert bytes(pf.read_page(pid)[24:28]) == b"MARK"  # file saw the write


def test_close_flushes_all_dirty_pages(tmp_path):
    path = tmp_path / "db.pico"
    pf = FilePageFile(path)
    pid = pf.alloc_page()
    pool = BufferPool(pf, capacity=8)
    page = pool.get(pid)
    page[0:4] = (12345).to_bytes(4, "big")
    pool.mark_dirty(pid)
    pool.close()
    # a brand-new stack reads what the old one flushed
    pf2 = FilePageFile(path)
    assert int.from_bytes(pf2.read_page(pid)[0:4], "big") == 12345
    pf2.close()


def test_mark_dirty_requires_resident_page():
    pool = BufferPool(MemoryPageFile(), capacity=2)
    with pytest.raises(PageError, match="non-resident"):
        pool.mark_dirty(0)


def test_truncated_file_rejected(tmp_path):
    path = tmp_path / "bad.pico"
    path.write_bytes(b"x" * 100)  # not a page multiple
    with pytest.raises(PageError, match="truncated"):
        FilePageFile(path)
