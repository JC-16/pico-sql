"""Second-pass adversarial review regressions (Day 1+2 hardening).

Each test locks a defect found by the second review pass:
R1 REPL split statements on ';' even inside open string literals
R2 REPL meta commands unusable / user trapped while a statement is pending
R3 corrupt catalog JSON escaped as raw json.JSONDecodeError
R4 FilePageFile leaked raw OSError on open failures
R5 Database.close() crashed on double close; execute-after-close leaked
   raw ValueError from a closed file handle
"""

import pytest

from picosql.engine import Database, EngineError
from picosql.lexer import tokenize
from picosql.repl import run_loop
from picosql.storage.bufferpool import BufferPool, FilePageFile
from picosql.storage.pages import PageError


def drive(lines, db=None):
    outputs = []
    it = iter(lines)
    run_loop(read_line=lambda _prompt: next(it), echo=outputs.append, database=db)
    return outputs


# ----------------------------------------------- R1: strings across REPL lines


def test_string_containing_semicolon_survives_repl():
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY, s VARCHAR(80));",
            "INSERT INTO t VALUES (1, 'line one;",
            ".quit is inside the string; really');",
            "SELECT s FROM t;",
            ".quit",
        ]
    )
    assert any("1 row(s)" in line for line in outputs)
    assert any("line one;" in line for line in outputs)


def test_string_with_escaped_quote_keeps_parity():
    # 'a''b;' has four quotes -> balanced -> the statement completes on ';'
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY, s VARCHAR(20));",
            "INSERT INTO t VALUES (1, 'a''b;');",
            "SELECT s FROM t;",
            ".quit",
        ]
    )
    assert any("a'b;" in line for line in outputs)


def test_quit_line_inside_open_string_is_content():
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY, s VARCHAR(80));",
            "INSERT INTO t VALUES (1, 'still;",
            ".quit');",  # inside the string: content, not a command
            "SELECT id FROM t;",
            ".quit",
        ]
    )
    # the session survived the embedded .quit line and executed everything
    assert any("1 row(s)" in line for line in outputs)


# --------------------------------------------- R2: meta with pending statement


def test_quit_works_with_pending_statement():
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY);",
            "INSERT INTO t VALUES (1",  # pending, no ';'
            ".quit",  # must abort, not be swallowed into the buffer
        ]
    )
    assert outputs[-1] != "       -> "  # loop actually returned


def test_meta_with_pending_statement_is_refused_not_executed():
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY);",
            "INSERT INTO t VALUES (1",  # pending
            ".tables",  # refused while pending
            ");",  # complete the statement anyway
            ".tables",  # now it runs and shows the table
            ".quit",
        ]
    )
    assert any(
        "cannot run meta commands while a statement is pending" in line for line in outputs
    )
    assert any("t" == line.strip() for line in outputs)


# --------------------------------------------- R3: corrupt catalog surfaced


def test_corrupt_catalog_becomes_engine_error(tmp_path):
    path = tmp_path / "corrupt.pico"
    db = Database(path)
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    db.close()
    # corrupt the catalog page (page 0): exactly 24 bytes of garbage so the
    # file length and page structure stay intact
    raw = bytearray(path.read_bytes())
    assert len(raw) % 4096 == 0
    raw[0:24] = b"x" * 24  # definitely not valid JSON, exactly 24 bytes
    assert len(raw) % 4096 == 0
    path.write_bytes(bytes(raw))
    with pytest.raises(EngineError, match="cannot open database"):
        Database(path)


# --------------------------------------------- R4: file open failure surfaced


def test_file_pagefile_on_directory_raises_page_error(tmp_path):
    with pytest.raises(PageError, match="cannot open database file"):
        FilePageFile(tmp_path)  # a directory, not a file


# --------------------------------------------- R5: close lifecycle hardening


def test_double_close_and_execute_after_close(tmp_path):
    db = Database(tmp_path / "life.pico")
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    db.execute_sql("INSERT INTO t VALUES (1);")
    db.close()
    db.close()  # must be a no-op, not a crash
    with pytest.raises(EngineError, match="database is closed"):
        db.execute_sql("SELECT * FROM t")


def test_pool_after_close_raises_page_error():
    from picosql.storage.bufferpool import MemoryPageFile

    pf = MemoryPageFile()
    pf.alloc_page()
    pool = BufferPool(pf, capacity=2)
    pool.close()
    with pytest.raises(PageError, match="closed"):
        pool.get(0)
    pool.close()  # idempotent


# --------------------------------------------- lexer behavior locked by test


def test_digit_leading_identifier_lexes_as_number_then_ident():
    # documented behavior: identifiers cannot start with a digit
    tokens = tokenize("1abc")
    assert [(t.type, t.value) for t in tokens] == [
        ("NUMBER", 1),
        ("IDENT", "abc"),
        ("EOF", None),
    ]
