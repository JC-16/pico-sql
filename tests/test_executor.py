from picosql.executor import (
    CountingOperator,
    FilterOperator,
    LimitOperator,
    SeqScanOperator,
    SortOperator,
)
from picosql.engine import Database


def make_db(n=20):
    db = Database()
    rows = ", ".join(f"({i}, 'name-{i}', {i * 1.5})" for i in range(n))
    db.execute_sql(
        f"""
        CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20), score FLOAT);
        INSERT INTO t VALUES {rows};
        """
    )
    return db


def test_limit_is_lazy_child_is_not_over_pulled():
    db = make_db(100)
    seq = SeqScanOperator(db.tables["t"])
    counter = CountingOperator(seq)
    limit = LimitOperator(counter, 5)
    rows = limit.drain()
    assert [r[0] for r in rows] == [0, 1, 2, 3, 4]
    # laziness proof: the limit pulled exactly 5 rows, not 100
    assert counter.pulled == 5


def test_limit_larger_than_table():
    db = make_db(10)
    seq = SeqScanOperator(db.tables["t"])
    counter = CountingOperator(seq)
    limit = LimitOperator(counter, 99)
    assert len(limit.drain()) == 10
    assert counter.pulled == 10


def test_sort_operator_is_blocking_but_correct():
    db = make_db(15)
    seq = SeqScanOperator(db.tables["t"])
    sort_op = SortOperator(seq, lambda row: row[2], reverse=True)
    rows = sort_op.drain()
    scores = [r[2] for r in rows]
    assert scores == sorted(scores, reverse=True)
    assert len(rows) == 15


def test_volcano_pipeline_end_to_end():
    db = make_db(30)
    table = db.tables["t"]
    seq = SeqScanOperator(table)
    filter_op = FilterOperator(seq, lambda row: row[0] % 2 == 0)
    sort_op = SortOperator(filter_op, lambda row: row[2], reverse=False)
    limit_op = LimitOperator(sort_op, 4)
    rows = limit_op.drain()
    evens = [i for i in range(30) if i % 2 == 0]
    scores = sorted(i * 1.5 for i in evens)
    assert [r[0] for r in rows] == [0, 2, 4, 6]
    assert [r[2] for r in rows] == scores[:4]
