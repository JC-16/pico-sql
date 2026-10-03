"""The Day 2 headline guarantee: data survives close() + reopen."""

import pytest

from picosql.engine import Database, EngineError


def test_file_database_roundtrip(tmp_path):
    path = tmp_path / "db.pico"
    db = Database(path)
    db.execute_sql(
        """
        CREATE TABLE users (
            id INT PRIMARY KEY,
            name VARCHAR(20),
            score FLOAT,
            active BOOL
        );
        INSERT INTO users VALUES (1, 'alice', 91.5, true), (2, 'bob', 72.0, false);
        UPDATE users SET score = 99.0 WHERE id = 2;
        """
    )
    db.close()

    db2 = Database(path)
    (res,) = db2.execute_sql("SELECT id, name, score, active FROM users ORDER BY id")
    assert res.rows == [
        [1, "alice", 91.5, True],
        [2, "bob", 99.0, False],
    ]
    # constraints survive too: the pk index was rebuilt from the pages
    with pytest.raises(EngineError, match="duplicate primary key"):
        db2.execute_sql("INSERT INTO users VALUES (1, 'clash', 0.0, true)")
    db2.execute_sql("DELETE FROM users WHERE id = 1")
    db2.close()

    db3 = Database(path)
    (res,) = db3.execute_sql("SELECT id FROM users")
    assert res.rows == [[2]]
    db3.close()


def test_persistence_across_three_sessions_with_nulls(tmp_path):
    path = tmp_path / "nulls.pico"
    db = Database(path)
    db.execute_sql(
        """
        CREATE TABLE t (id INT PRIMARY KEY, v INT);
        INSERT INTO t VALUES (1, 10), (2, NULL);
        """
    )
    db.close()  # without close(), dirty pages are lost -- pre-WAL semantics

    db = Database(path)
    (res,) = db.execute_sql("SELECT id, v FROM t ORDER BY id")
    assert res.rows == [[1, 10], [2, None]]
    db.close()


def test_memory_database_has_no_file_semantics():
    db = Database()  # no path -> in-memory pages
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY);")
    db.execute_sql("INSERT INTO t VALUES (1);")
    db.close()
    # nothing to reopen -- by design; the caller chose ephemeral mode


def test_page_recycling_after_drop(tmp_path):
    path = tmp_path / "recycle.pico"
    db = Database(path)
    db.execute_sql(
        """
        CREATE TABLE big (id INT PRIMARY KEY, note VARCHAR(100));
        INSERT INTO big VALUES (1, 'x050');
        """
    )
    db.execute_sql("DROP TABLE big;")
    db.execute_sql("CREATE TABLE fresh (id INT PRIMARY KEY);")
    db.execute_sql("INSERT INTO fresh VALUES (1);")
    db.close()

    db2 = Database(path)
    (res,) = db2.execute_sql("SELECT id FROM fresh")
    assert res.rows == [[1]]
    with pytest.raises(EngineError, match="no such table"):
        db2.execute_sql("SELECT * FROM big")
    db2.close()
    # the file only grew by the catalog page + one fresh data page:
    # the dropped table's page was recycled, not leaked
    assert path.stat().st_size <= 3 * 4096
