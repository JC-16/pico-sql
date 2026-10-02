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
