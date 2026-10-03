import pytest

from picosql import EngineError
from picosql.engine import Database


def make_db():
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE users (
            id INT PRIMARY KEY,
            name VARCHAR(20),
            score FLOAT
        );
        INSERT INTO users VALUES (1, 'alice', 91.5), (2, 'bob', 72.0), (3, 'carol', 58.0);
        """
    )
    return db


def test_select_all_and_projection():
    db = make_db()
    (res,) = db.execute_sql("SELECT * FROM users")
    assert res.columns == ["id", "name", "score"]
    assert len(res.rows) == 3
    (res,) = db.execute_sql("SELECT name FROM users WHERE id = 2")
    assert res.rows == [["bob"]]


def test_where_with_and_or():
    db = make_db()
    (res,) = db.execute_sql("SELECT name FROM users WHERE score >= 60 AND id = 1")
    assert res.rows == [["alice"]]
    (res,) = db.execute_sql("SELECT name FROM users WHERE id = 1 OR id = 3")
    assert [r[0] for r in res.rows] == ["alice", "carol"]


def test_order_by_and_limit():
    db = make_db()
    (res,) = db.execute_sql("SELECT name FROM users ORDER BY score DESC LIMIT 2")
    assert [r[0] for r in res.rows] == ["alice", "bob"]


def test_arithmetic_in_where():
    db = make_db()
    (res,) = db.execute_sql("SELECT name FROM users WHERE score / 2 > 40")
    # 91.5 / 2 = 45.75 > 40 matches; 72 / 2 = 36 and 58 / 2 = 29 do not.
    assert [r[0] for r in res.rows] == ["alice"]


def test_update_and_delete():
    db = make_db()
    (res,) = db.execute_sql("UPDATE users SET score = 65 WHERE name = 'carol'")
    assert res.affected == 1
    (res,) = db.execute_sql("SELECT score FROM users WHERE name = 'carol'")
    assert res.rows == [[65.0]]  # FLOAT column coerces int literal
    (res,) = db.execute_sql("DELETE FROM users WHERE id = 2")
    assert res.affected == 1
    (res,) = db.execute_sql("SELECT id FROM users")
    assert [r[0] for r in res.rows] == [1, 3]


def test_primary_key_duplicate_rejected():
    db = make_db()
    with pytest.raises(EngineError, match="duplicate primary key"):
        db.execute_sql("INSERT INTO users VALUES (1, 'dup', 10.0)")


def test_type_violation_rejected():
    db = make_db()
    with pytest.raises(EngineError, match="expects INT"):
        db.execute_sql("INSERT INTO users VALUES ('x', 'y', 1.0)")
    with pytest.raises(EngineError, match="too long"):
        db.execute_sql("INSERT INTO users VALUES (9, '0123456789012345678901234', 1.0)")


def test_null_semantics_in_where():
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE t (id INT PRIMARY KEY, v INT);
        INSERT INTO t VALUES (1, 10), (2, NULL);
        """
    )
    # NULL comparison is NULL -> not matched by WHERE (three-valued logic)
    (res,) = db.execute_sql("SELECT id FROM t WHERE v = 10 OR v <> 10")
    assert [r[0] for r in res.rows] == [1]
    # arithmetic with NULL propagates NULL -> not matched
    (res,) = db.execute_sql("SELECT id FROM t WHERE v + 1 = 11")
    assert [r[0] for r in res.rows] == [1]


def test_null_sorts_first_ascending():
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE t (id INT PRIMARY KEY, v INT);
        INSERT INTO t VALUES (1, 5), (2, NULL), (3, 1);
        """
    )
    (res,) = db.execute_sql("SELECT id FROM t ORDER BY v")
    assert [r[0] for r in res.rows] == [2, 3, 1]
    (res,) = db.execute_sql("SELECT id FROM t ORDER BY v DESC")
    assert [r[0] for r in res.rows] == [1, 3, 2]


def test_update_primary_key_maintains_index():
    db = make_db()
    db.execute_sql("UPDATE users SET id = 99 WHERE id = 1")
    with pytest.raises(EngineError, match="duplicate primary key"):
        db.execute_sql("INSERT INTO users VALUES (99, 'clash', 1.0)")
    (res,) = db.execute_sql("SELECT name FROM users WHERE id = 99")
    assert res.rows == [["alice"]]


def test_drop_table_and_missing_table_error():
    db = make_db()
    db.execute_sql("DROP TABLE users")
    with pytest.raises(EngineError, match="no such table"):
        db.execute_sql("SELECT * FROM users")
    # re-creating a dropped table is legal
    db.execute_sql("CREATE TABLE users (id INT)")
    assert "users" in db.tables


def test_create_duplicate_table_raises():
    db = make_db()
    with pytest.raises(EngineError, match="already exists"):
        db.execute_sql("CREATE TABLE users (id INT)")


def test_bool_column_roundtrip():
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE flags (id INT PRIMARY KEY, ok BOOL);
        INSERT INTO flags VALUES (1, true), (2, false);
        """
    )
    (res,) = db.execute_sql("SELECT ok FROM flags WHERE id = 1")
    assert res.rows == [[True]]


# ------------------------------------------------- strict-review regressions


def test_int64_overflow_rejected_as_engine_error():
    db = make_db()
    with pytest.raises(EngineError, match="INT64 range"):
        db.execute_sql(f"INSERT INTO users VALUES ({2**63}, 'big', 1.0)")
    with pytest.raises(EngineError, match="INT64 range"):
        db.execute_sql(f"INSERT INTO users VALUES ({-(2**63) - 1}, 'big', 1.0)")
    # produced by arithmetic, not just literals -- coercion is the gate
    with pytest.raises(EngineError, match="INT64 range"):
        db.execute_sql(f"INSERT INTO users VALUES ({2**63 - 1} + 1, 'big', 1.0)")
    # boundary values are legal and persist
    db.execute_sql(f"INSERT INTO users VALUES ({2**63 - 1}, 'max', 1.0)")
    db.execute_sql(f"INSERT INTO users VALUES ({-(2**63)}, 'min', 1.0)")
    (res,) = db.execute_sql("SELECT id FROM users WHERE name = 'max'")
    assert res.rows == [[2**63 - 1]]


def test_multi_row_insert_is_atomic():
    db = make_db()
    with pytest.raises(EngineError, match="duplicate primary key"):
        db.execute_sql("INSERT INTO users VALUES (10, 'x', 1.0), (10, 'y', 2.0)")
    # phase 1 must have written NOTHING: row (10,'x') must not exist
    (res,) = db.execute_sql("SELECT * FROM users WHERE id = 10")
    assert res.rows == []
    (res,) = db.execute_sql("SELECT id FROM users")
    assert len(res.rows) == 3  # the original three rows, untouched


def test_insert_failure_mid_statement_writes_nothing():
    db = make_db()
    with pytest.raises(EngineError, match="expects INT"):
        db.execute_sql("INSERT INTO users VALUES (10, 'ok', 1.0), ('bad', 'no', 2.0)")
    (res,) = db.execute_sql("SELECT id FROM users WHERE id = 10")
    assert res.rows == []  # the valid first row was staged, never committed


def test_update_oversize_varchar_is_atomic():
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE t (id INT PRIMARY KEY, note VARCHAR);
        INSERT INTO t VALUES (1, 'short'), (2, 'also short');
        """
    )
    with pytest.raises(EngineError, match="never fit"):
        db.execute_sql(f"UPDATE t SET note = '{'x' * 5000}' WHERE id >= 1")
    (res,) = db.execute_sql("SELECT note FROM t ORDER BY id")
    assert res.rows == [["short"], ["also short"]]  # no partial commit


def test_primary_key_allows_null_documented_deviation():
    # KNOWN DEVIATION from standard SQL: PRIMARY KEY does not imply NOT NULL
    # here, and multiple NULL keys are allowed (each is simply unindexed).
    # Real engines reject the first INSERT outright.
    db = Database()
    db.execute_sql(
        """
        CREATE TABLE t (id INT PRIMARY KEY, v INT);
        INSERT INTO t VALUES (NULL, 1), (NULL, 2);
        """
    )
    (res,) = db.execute_sql("SELECT v FROM t WHERE v > 0")
    assert res.rows == [[1], [2]]
