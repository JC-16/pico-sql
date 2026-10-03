"""Slotted page layout: the unit of storage between memory and disk.

One page is PAGE_SIZE bytes with two-ended growth::

    offset 0                     offset PAGE_SIZE
    +-----------------------------------------------+
    | header (24 B)                                 |
    +-----------------------------------------------+
    | slot directory: 4 B per slot, grows forward   |
    |   slot i = (u16 offset, u16 length)           |
    |   tombstone = (0, 0) -- offset 0 lives in the |
    |   header, so it can never start a record      |
    +-----------------------------------------------+ <- data_start
    |            free space                         |
    +-----------------------------------------------+
    | records, packed backward from the page end    |
    +-----------------------------------------------+

Why two-ended growth?  The slot directory must grow when records are added,
but moving it would invalidate record offsets. Packing records from the far
end means inserting a record only moves ``data_start`` -- existing records
never move, and existing slot numbers never change.

Slot numbers are stable forever: compaction rewrites record *offsets* but
keeps the slot numbering, so external references (row_id = page_id + slot_no)
stay valid across compaction. Deleted slots become tombstones and are reused
by the next insert.

All integers are big-endian (network order) for byte-level determinism.
"""

from __future__ import annotations

import struct
from typing import Iterator, Optional

PAGE_SIZE = 4096
HEADER_SIZE = 24
SLOT_SIZE = 4
PAGE_TYPE_DATA = 1

# header offsets
_H_PAGE_ID = 0  # >I
_H_NUM_SLOTS = 4  # >H
_H_NUM_LIVE = 6  # >H
_H_DATA_START = 8  # >H
_H_PAGE_TYPE = 10  # >H


class PageError(Exception):
    """Raised for page-level problems: corruption, no space, bad slot."""


def new_page(page_id: int, page_type: int = PAGE_TYPE_DATA) -> bytearray:
    """A fresh, valid page with no slots and no records."""
    data = bytearray(PAGE_SIZE)
    struct.pack_into(">I", data, _H_PAGE_ID, page_id)
    struct.pack_into(">H", data, _H_DATA_START, PAGE_SIZE)
    struct.pack_into(">H", data, _H_PAGE_TYPE, page_type)
    return data


def page_id(data: bytearray) -> int:
    return struct.unpack_from(">I", data, _H_PAGE_ID)[0]


def num_slots(data: bytearray) -> int:
    return struct.unpack_from(">H", data, _H_NUM_SLOTS)[0]


def num_live(data: bytearray) -> int:
    return struct.unpack_from(">H", data, _H_NUM_LIVE)[0]


def data_start(data: bytearray) -> int:
    return struct.unpack_from(">H", data, _H_DATA_START)[0]


def _set_num_slots(data: bytearray, value: int) -> None:
    struct.pack_into(">H", data, _H_NUM_SLOTS, value)


def _set_num_live(data: bytearray, value: int) -> None:
    struct.pack_into(">H", data, _H_NUM_LIVE, value)


def _set_data_start(data: bytearray, value: int) -> None:
    struct.pack_into(">H", data, _H_DATA_START, value)


def validate(data: bytearray, expected_page_id: Optional[int] = None) -> None:
    """Sanity-check the page structure; raises PageError on corruption."""
    if len(data) != PAGE_SIZE:
        raise PageError(f"page size is {len(data)}, expected {PAGE_SIZE}")
    n = num_slots(data)
    slots_end = HEADER_SIZE + n * SLOT_SIZE
    ds = data_start(data)
    if slots_end > PAGE_SIZE:
        raise PageError("corrupt page: slot directory overruns the page")
    if not HEADER_SIZE + n * SLOT_SIZE <= ds <= PAGE_SIZE:
        raise PageError("corrupt page: data_start out of range")
    if expected_page_id is not None and page_id(data) != expected_page_id:
        raise PageError(
            f"page id mismatch: header says {page_id(data)}, expected {expected_page_id}"
        )


def slot(data: bytearray, slot_no: int) -> tuple:
    """Return (offset, length) of a slot. Tombstones are (0, 0)."""
    n = num_slots(data)
    if not 0 <= slot_no < n:
        raise PageError(f"slot {slot_no} out of range (page has {n} slots)")
    return struct.unpack_from(">HH", data, HEADER_SIZE + slot_no * SLOT_SIZE)


def free_space(data: bytearray) -> int:
    """Bytes available between the slot directory and the record area."""
    return data_start(data) - (HEADER_SIZE + num_slots(data) * SLOT_SIZE)


def insert_record(data: bytearray, record: bytes) -> int:
    """Insert a record; returns its slot number. Raises PageError if full.

    Reuses the first tombstone slot when one exists (no directory growth),
    otherwise appends a new slot.
    """
    validate(data)
    rec_len = len(record)
    if rec_len == 0:
        raise PageError("cannot store an empty record")
    if rec_len > PAGE_SIZE - HEADER_SIZE:
        raise PageError("record larger than a page")

    n = num_slots(data)
    reuse = -1
    for i in range(n):
        off, length = slot(data, i)
        if off == 0 and length == 0:
            reuse = i
            break

    new_n = n if reuse >= 0 else n + 1
    if HEADER_SIZE + new_n * SLOT_SIZE > PAGE_SIZE:
        raise PageError("page full: slot directory has no room")
    need = rec_len if reuse >= 0 else rec_len + SLOT_SIZE
    if free_space(data) < need:
        raise PageError("page full")

    new_ds = data_start(data) - rec_len
    data[new_ds : new_ds + rec_len] = record
    slot_no = reuse if reuse >= 0 else n
    struct.pack_into(">HH", data, HEADER_SIZE + slot_no * SLOT_SIZE, new_ds, rec_len)
    _set_num_slots(data, new_n)
    _set_data_start(data, new_ds)
    _set_num_live(data, num_live(data) + 1)
    return slot_no


def delete_record(data: bytearray, slot_no: int) -> None:
    """Tombstone a slot. The bytes stay until compaction reclaims them."""
    validate(data)
    off, length = slot(data, slot_no)
    if off == 0 and length == 0:
        raise PageError(f"slot {slot_no} is already deleted")
    struct.pack_into(">HH", data, HEADER_SIZE + slot_no * SLOT_SIZE, 0, 0)
    _set_num_live(data, num_live(data) - 1)


def read_record(data: bytearray, slot_no: int) -> Optional[bytes]:
    """Return the record bytes, or None if the slot is a tombstone."""
    off, length = slot(data, slot_no)
    if off == 0 and length == 0:
        return None
    return bytes(data[off : off + length])


def update_record_inplace(data: bytearray, slot_no: int, record: bytes) -> bool:
    """Overwrite a live slot in place when the new record fits the slot's
    current bytes. Returns False if the caller must tombstone + re-insert.

    A shorter record leaves a dead gap before the next record's offset --
    harmless (data_start only ever moves down), and compaction reclaims it.
    """
    validate(data)
    offset, old_len = slot(data, slot_no)
    if offset == 0 and old_len == 0:
        raise PageError("cannot update a deleted slot")
    if len(record) > old_len:
        return False
    data[offset : offset + len(record)] = record
    struct.pack_into(">HH", data, HEADER_SIZE + slot_no * SLOT_SIZE, offset, len(record))
    return True


def iterate_live(data: bytearray) -> Iterator:
    """Yield (slot_no, record_bytes) for every live record, in slot order."""
    for i in range(num_slots(data)):
        rec = read_record(data, i)
        if rec is not None:
            yield i, rec


def compact(data: bytearray) -> int:
    """Rewrite live records contiguously at the page end.

    Slot numbers are preserved (tombstones stay in place as tombstones), so
    row_id references held elsewhere remain valid. Returns bytes reclaimed.
    """
    validate(data)
    old_ds = data_start(data)
    live = [(i, read_record(data, i)) for i in range(num_slots(data))]
    live = [(i, rec) for i, rec in live if rec is not None]

    total = sum(len(rec) for _, rec in live)
    fresh = bytearray(PAGE_SIZE)
    struct.pack_into(">I", fresh, _H_PAGE_ID, page_id(data))
    struct.pack_into(">H", fresh, _H_NUM_SLOTS, num_slots(data))
    struct.pack_into(">H", fresh, _H_NUM_LIVE, len(live))
    struct.pack_into(">H", fresh, _H_PAGE_TYPE, struct.unpack_from(">H", data, _H_PAGE_TYPE)[0])
    pos = PAGE_SIZE
    for i, rec in live:  # slot order, packed from the page end backward
        pos -= len(rec)
        fresh[pos : pos + len(rec)] = rec
        struct.pack_into(">HH", fresh, HEADER_SIZE + i * SLOT_SIZE, pos, len(rec))
    struct.pack_into(">H", fresh, _H_DATA_START, pos)

    data[:] = fresh
    return (PAGE_SIZE - old_ds) - total
