import pytest

from picosql.engine import Database, EngineError
from picosql.repl import run_loop


def drive(lines, db=None):
    """Feed input lines to the REPL and collect echoed output."""
    outputs = []
    it = iter(lines)
    run_loop(
        read_line=lambda _prompt: next(it),
        echo=outputs.append,
        database=db,
    )
    return outputs


def test_full_session_flow():
    outputs = drive(
        [
            "CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(10));",
            "INSERT INTO t VALUES (1, 'alice'), (2, 'bob');",
            "SELECT * FROM t;",
            ".tables",
            ".quit",
        ]
    )
    assert outputs[0].startswith("pico-sql")
    assert any("2 row(s)" in line for line in outputs)
    assert any("alice" in line for line in outputs)
    assert any("t" == line.strip() for line in outputs)


def test_multiline_statement():
    outputs = drive(
        [
            "CREATE TABLE t (id INT",
            "  PRIMARY KEY);",
            "INSERT INTO t VALUES (7);",
            "SELECT id FROM t;",
            ".quit",
        ]
    )
    assert any("7" in line for line in outputs)


def test_error_is_reported_not_raised():
    outputs = drive(
        [
            "SELECT * FROM missing;",
            "SELEC nonsense;",
            ".quit",
        ]
    )
    errors = [line for line in outputs if line.startswith("ERROR")]
    assert len(errors) == 2


def test_meta_help_and_unknown():
    outputs = drive([".help", ".bogus", ".quit"])
    assert any(".quit" in line for line in outputs)
    assert any("unknown command" in line for line in outputs)


def test_incomplete_statement_keeps_buffer_then_eof():
    outputs = drive(["SELECT *", "FROM t"])  # never terminated with ';'
    assert outputs  # banner only; loop exits on EOF without crashing


def test_database_state_survives_via_returned_object():
    db = Database()
    drive(["CREATE TABLE k (id INT PRIMARY KEY);", "INSERT INTO k VALUES (1);", ".quit"], db=db)
    assert "k" in db.tables
    assert db.tables["k"].rows == [[1]]


def test_engine_error_type_stable():
    db = Database()
    with pytest.raises(EngineError):
        db.execute_sql("SELECT * FROM nothing")
