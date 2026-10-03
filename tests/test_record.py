import pytest

from picosql.storage.pages import PageError
from picosql.storage.record import decode_row, encode_row, max_record_size


class FakeColumn:
    def __init__(self, ctype, varchar_len=None):
        self.type = ctype
        self.varchar_len = varchar_len


def test_roundtrip_all_types():
    columns = [FakeColumn("INT"), FakeColumn("FLOAT"), FakeColumn("BOOL"), FakeColumn("VARCHAR")]
    row = [123456789, 3.25, True, "hello 世界"]
    data = encode_row(columns, row)
    assert decode_row(columns, data) == row


def test_null_flags_per_column():
    columns = [FakeColumn("INT"), FakeColumn("VARCHAR")]
    row = [None, "abc"]
    data = encode_row(columns, row)
    # INT: 1 flag byte only; VARCHAR: 1 flag + 2 len + 3 bytes
    assert data == b"\x00" + b"\x01\x00\x03" + b"abc"
    assert decode_row(columns, data) == [None, "abc"]


def test_all_null_row():
    columns = [FakeColumn("INT"), FakeColumn("FLOAT"), FakeColumn("BOOL")]
    data = encode_row(columns, [None, None, None])
    assert data == b"\x00\x00\x00"
    assert decode_row(columns, data) == [None, None, None]


def test_empty_string_vs_null():
    columns = [FakeColumn("VARCHAR")]
    assert decode_row(columns, encode_row(columns, [""])) == [""]
    assert decode_row(columns, encode_row(columns, [None])) == [None]
    assert len(encode_row(columns, [""])) == 3  # flag + u16 length 0
    assert len(encode_row(columns, [None])) == 1


def test_column_count_mismatch_raises():
    columns = [FakeColumn("INT")]
    with pytest.raises(PageError, match="columns"):
        encode_row(columns, [1, 2])


def test_truncated_record_detected():
    columns = [FakeColumn("INT"), FakeColumn("INT")]
    data = encode_row(columns, [1, 2])
    with pytest.raises(PageError, match="truncated"):
        decode_row(columns, data[:-3])


def test_trailing_bytes_detected():
    columns = [FakeColumn("INT")]
    data = encode_row(columns, [1]) + b"garbage"
    with pytest.raises(PageError, match="trailing"):
        decode_row(columns, data)


def test_corrupt_null_flag_detected():
    columns = [FakeColumn("INT")]
    with pytest.raises(PageError, match="null flag"):
        decode_row(columns, b"\x07" + b"\x00" * 8)


def test_max_record_size_formula():
    assert max_record_size() < 4096
