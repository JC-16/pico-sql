import pytest

from picosql.storage.pages import (
    HEADER_SIZE,
    PAGE_SIZE,
    PageError,
    compact,
    delete_record,
    free_space,
    insert_record,
    iterate_live,
    new_page,
    num_slots,
    read_record,
    slot,
    update_record_inplace,
    validate,
)


def make_filled_page(n: int):
    page = new_page(7)
    for i in range(n):
        insert_record(page, f"record-{i}".encode())
    return page


def test_new_page_is_valid_and_empty():
    page = new_page(42)
    validate(page, expected_page_id=42)
    assert num_slots(page) == 0
    assert free_space(page) == PAGE_SIZE - HEADER_SIZE


def test_insert_and_read_roundtrip():
    page = new_page(1)
    s0 = insert_record(page, b"hello")
    s1 = insert_record(page, b"world!")
    assert (s0, s1) == (0, 1)
    assert read_record(page, 0) == b"hello"
    assert read_record(page, 1) == b"world!"
    validate(page, expected_page_id=1)  # page id survives writes


def test_insert_fills_page_then_raises():
    page = new_page(1)
    count = 0
    while True:
        try:
            insert_record(page, b"xy")  # 2 bytes + 4-byte slot
            count += 1
        except PageError as exc:
            assert "page full" in str(exc)
            break
    # every byte usable was consumed
    assert free_space(page) < 2 + 4
    assert count > 500  # (4096-24) / 6 = 678 slots max


def test_delete_tombstones_and_read_returns_none():
    page = make_filled_page(3)
    delete_record(page, 1)
    assert read_record(page, 1) is None
    assert read_record(page, 0) == b"record-0"
    assert read_record(page, 2) == b"record-2"
    with pytest.raises(PageError, match="already deleted"):
        delete_record(page, 1)


def test_tombstone_slot_is_reused():
    page = make_filled_page(3)
    delete_record(page, 1)
    s = insert_record(page, b"new")
    assert s == 1  # reused, directory did not grow
    assert num_slots(page) == 3


def test_compaction_keeps_slot_numbers_stable():
    page = make_filled_page(5)
    free_before = free_space(page)
    delete_record(page, 0)
    delete_record(page, 3)
    reclaimed = compact(page)
    assert reclaimed == 16  # two tombstoned 8-byte records
    assert free_space(page) == free_before + reclaimed
    assert read_record(page, 0) is None      # tombstone stays a tombstone
    assert read_record(page, 1) == b"record-1"
    assert read_record(page, 2) == b"record-2"
    assert read_record(page, 3) is None
    assert read_record(page, 4) == b"record-4"


def test_compact_then_insert_fills_reclaimed_space():
    page = make_filled_page(4)
    delete_record(page, 0)
    delete_record(page, 1)
    delete_record(page, 2)
    compact(page)
    big = b"z" * 30
    insert_record(page, big)
    assert read_record(page, 0) == big  # reused tombstone slot 0


def test_update_in_place_shorter_record():
    page = make_filled_page(2)
    ok = update_record_inplace(page, 0, b"LONGER-THAN-EIGHT")  # record-0 is 8 bytes
    assert ok is False
    ok = update_record_inplace(page, 0, b"xy")
    assert ok is True
    assert read_record(page, 0) == b"xy"
    # the 6 dead bytes are invisible: scan still returns only live records
    assert [r for _, r in iterate_live(page)] == [b"xy", b"record-1"]


def test_page_id_mismatch_detected():
    page = new_page(1)
    with pytest.raises(PageError, match="mismatch"):
        validate(page, expected_page_id=99)


def test_corrupt_page_rejected():
    page = new_page(1)
    import struct

    struct.pack_into(">H", page, 8, 60000)  # data_start beyond page size
    with pytest.raises(PageError, match="corrupt"):
        validate(page)


def test_slot_out_of_range():
    page = make_filled_page(2)
    with pytest.raises(PageError, match="out of range"):
        slot(page, 5)
    with pytest.raises(PageError, match="out of range"):
        read_record(page, 5)


def test_zeroed_page_is_not_treated_as_valid():
    page = bytearray(PAGE_SIZE)
    with pytest.raises(PageError):
        insert_record(page, b"data")
