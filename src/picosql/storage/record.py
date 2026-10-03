"""Row <-> bytes codec.

Encoding per column: 1-byte NULL flag (0x00 = NULL, 0x01 = value present)
followed by the payload:
    INT     -> 8-byte big-endian signed
    FLOAT   -> 8-byte big-endian IEEE 754 double
    BOOL    -> 1 byte (0/1)
    VARCHAR -> 2-byte big-endian length + UTF-8 bytes

The NULL flag costs one byte per column; a real engine packs nulls into a
bitmap (see STUDY.md Day 2 exercises). Decoding validates that the record
is consumed exactly -- trailing bytes mean corruption.
"""

from __future__ import annotations

import struct

from .pages import PageError

NULL_FLAG = 0x00
VALUE_FLAG = 0x01


def encode_row(columns, row: list) -> bytes:
    """Encode one row (list of Python values) against the given schema."""
    if len(columns) != len(row):
        raise PageError(
            f"schema has {len(columns)} columns, row has {len(row)} values"
        )
    parts: list = []
    for col, value in zip(columns, row):
        if value is None:
            parts.append(b"\x00")
            continue
        parts.append(b"\x01")
        ctype = col.type
        if ctype == "INT":
            parts.append(struct.pack(">q", value))
        elif ctype == "FLOAT":
            parts.append(struct.pack(">d", value))
        elif ctype == "BOOL":
            parts.append(struct.pack(">B", 1 if value else 0))
        elif ctype == "VARCHAR":
            raw = value.encode("utf-8")
            if len(raw) > 0xFFFF:
                raise PageError("VARCHAR value longer than 65535 bytes")
            parts.append(struct.pack(">H", len(raw)))
            parts.append(raw)
        else:  # pragma: no cover - parser restricts types already
            raise PageError(f"unknown column type {ctype!r}")
    return b"".join(parts)


def _require(data: bytes, pos: int, size: int) -> None:
    if pos + size > len(data):
        raise PageError("corrupt record: truncated before all columns")


def decode_row(columns, data: bytes) -> list:
    """Decode record bytes back into a Python list. Raises PageError on
    truncation or corruption."""
    values: list = []
    pos = 0
    for col in columns:
        _require(data, pos, 1)
        flag = data[pos]
        pos += 1
        if flag == NULL_FLAG:
            values.append(None)
            continue
        if flag != VALUE_FLAG:
            raise PageError(f"corrupt record: bad null flag {flag:#x}")
        ctype = col.type
        if ctype == "INT":
            _require(data, pos, 8)
            values.append(struct.unpack_from(">q", data, pos)[0])
            pos += 8
        elif ctype == "FLOAT":
            _require(data, pos, 8)
            values.append(struct.unpack_from(">d", data, pos)[0])
            pos += 8
        elif ctype == "BOOL":
            _require(data, pos, 1)
            values.append(data[pos] == 1)
            pos += 1
        elif ctype == "VARCHAR":
            _require(data, pos, 2)
            (length,) = struct.unpack_from(">H", data, pos)
            pos += 2
            _require(data, pos, length)
            raw = data[pos : pos + length]
            values.append(raw.decode("utf-8"))
            pos += length
        else:  # pragma: no cover
            raise PageError(f"unknown column type {ctype!r}")
    if pos != len(data):
        raise PageError("corrupt record: trailing bytes after last column")
    return values


def max_record_size() -> int:
    """Largest record that can live in a data page (slot directory aside)."""
    from .pages import HEADER_SIZE, PAGE_SIZE, SLOT_SIZE

    return PAGE_SIZE - HEADER_SIZE - SLOT_SIZE
