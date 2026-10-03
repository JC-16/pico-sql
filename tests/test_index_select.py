import pytest

from picosql.engine import Database, EngineError, _extract_pk_plan, _split_conjunction
from picosql.parser import parse


def make_db(n=200):
    db = Database()
    rows = ", ".join(f"({i}, 'name-{i}', {i % 10})" for i in range(n))
    db.execute_sql(
        f"""
        CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20), grp INT);
        INSERT INTO t VALUES {rows};
        """
    )
    return db


def factors_of(db, where_sql):
    (stmt,) = parse(f"SELECT * FROM t WHERE {where_sql}")
    return _split_conjunction(stmt.where)


# --------------------------------------------------------------- the planner


def test_planner_point_lookup():
    db = make_db(50)
    plan, residual = _extract_pk_plan(db.tables["t"], factors_of(db, "id = 7"))
    assert plan is not None
    assert residual == []
    assert plan(db.tables["t"]).drain() == [[7, "name-7", 7]]


def test_planner_range_bounds():
    db = make_db(50)
    table = db.tables["t"]
    plan, residual = _extract_pk_plan(table, factors_of(db, "id > 10 AND id <= 13"))
    assert plan is not None
    assert residual == []
    rows = plan(table).drain()
    assert [r[0] for r in rows] == [11, 12, 13]


def test_planner_mirrored_literal_on_left():
    db = make_db(50)
    table = db.tables["t"]
    plan, _ = _extract_pk_plan(table, factors_of(db, "25 >= id"))
    assert plan is not None
    rows = plan(table).drain()
    assert [r[0] for r in rows] == list(range(26))  # mirrored >= includes 25


def test_planner_leaves_residual_for_non_pk_conditions():
    db = make_db(50)
    table = db.tables["t"]
    plan, residual = _extract_pk_plan(table, factors_of(db, "id >= 40 AND grp = 3"))
    assert plan is not None  # the id bound IS usable
    assert len(residual) == 1  # grp = 3 stays in the residual for FilterOperator
    # the plan operator alone resolves ONLY the id bound:
    rows = plan(table).drain()
    assert [r[0] for r in rows] == list(range(40, 50))
    # the full engine pipeline applies the residual on top:
    (res,) = db.execute_sql("SELECT id FROM t WHERE id >= 40 AND grp = 3")
    assert db.last_scan_used_index is True
    assert res.rows == [[43]]


def test_planner_rejects_or_and_column_comparisons():
    db = make_db(50)
    table = db.tables["t"]
    or_expr = factors_of(db, "id = 5 OR id = 6")[0]
    plan, residual = _extract_pk_plan(table, [or_expr])
    assert plan is None  # OR cannot use a single range
    assert residual == [or_expr]
    plan, _ = _extract_pk_plan(table, factors_of(db, "id = grp"))
    assert plan is None  # column vs column: no literal bound


def test_planner_rejects_mixed_type_bounds():
    db = make_db(50)
    table = db.tables["t"]
    plan, _ = _extract_pk_plan(table, factors_of(db, "id > 'a' AND id < 10"))
    assert plan is None  # string lower bound vs integer upper bound: unusable


# ------------------------------------------------------- engine integration


def test_engine_select_uses_index_and_is_correct():
    db = make_db(300)
    (res,) = db.execute_sql("SELECT name FROM t WHERE id = 250")
    assert db.last_scan_used_index is True
    assert res.rows == [["name-250"]]

    (res,) = db.execute_sql("SELECT id FROM t WHERE id > 290")
    assert db.last_scan_used_index is True
    assert [r[0] for r in res.rows] == list(range(291, 300))

    (res,) = db.execute_sql("SELECT id FROM t WHERE grp = 7")
    assert db.last_scan_used_index is False  # non-PK column: full scan
    assert [r[0] for r in res.rows] == [i for i in range(300) if i % 10 == 7]


def test_index_scan_with_residual_filter():
    db = make_db(100)
    (res,) = db.execute_sql("SELECT id FROM t WHERE id >= 90 AND grp = 5")
    assert db.last_scan_used_index is True
    assert res.rows == [[95]]


def test_index_correctness_after_update_and_delete():
    db = make_db(50)
    db.execute_sql("UPDATE t SET id = 500 WHERE id = 10")
    db.execute_sql("DELETE FROM t WHERE id = 20")
    (res,) = db.execute_sql("SELECT name FROM t WHERE id = 500")
    assert db.last_scan_used_index is True
    assert res.rows == [["name-10"]]
    (res,) = db.execute_sql("SELECT name FROM t WHERE id = 20")
    assert res.rows == []
    with pytest.raises(EngineError, match="duplicate primary key"):
        db.execute_sql("INSERT INTO t VALUES (500, 'clash', 0)")


def test_update_two_rows_to_same_pk_rejected_atomically():
    db = make_db(50)
    with pytest.raises(EngineError, match="duplicate primary key"):
        db.execute_sql("UPDATE t SET id = 1000 WHERE id = 1 OR id = 2")
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 1000")
    assert res.rows == []  # phase 1 caught the clash; phase 2 never ran
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 1")
    assert res.rows == [[1]]  # untouched
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 2")
    assert res.rows == [[2]]  # untouched


def test_index_persistence_via_rebuild(tmp_path):
    path = tmp_path / "idx.pico"
    db = Database(path)
    db.execute_sql("CREATE TABLE t (id INT PRIMARY KEY, v INT);")
    db.execute_sql("INSERT INTO t VALUES (5, 50), (1, 10), (9, 90);")
    db.close()
    db2 = Database(path)
    (res,) = db2.execute_sql("SELECT v FROM t WHERE id = 5")
    assert db2.last_scan_used_index is True
    assert res.rows == [[50]]
    db2.close()


# ------------------------------------------ adversarial regressions (Day 3)


def test_planner_neq_is_never_a_bound():
    """BUG-A regression: 'id != 5' was mis-planned as an upper bound and
    silently dropped every row above 5."""
    db = make_db(50)
    plan, residual = _extract_pk_plan(db.tables["t"], factors_of(db, "id != 25"))
    assert plan is None  # a disequality is not a range bound
    assert len(residual) == 1
    (res,) = db.execute_sql("SELECT id FROM t WHERE id != 25")
    assert db.last_scan_used_index is False
    assert [r[0] for r in res.rows] == [i for i in range(50) if i != 25]


def test_planner_tightens_multiple_lower_bounds():
    """BUG-B regression: the looser bound used to overwrite the tighter one."""
    db = make_db(50)
    (res,) = db.execute_sql("SELECT id FROM t WHERE id > 10 AND id > 20")
    assert db.last_scan_used_index is True
    assert [r[0] for r in res.rows] == list(range(21, 50))
    # looser bound written first, tighter second -- order must not matter
    (res,) = db.execute_sql("SELECT id FROM t WHERE id > 20 AND id > 10")
    assert [r[0] for r in res.rows] == list(range(21, 50))
    # inclusivity: a strict bound wins over an inclusive tie
    (res,) = db.execute_sql("SELECT id FROM t WHERE id >= 20 AND id > 20")
    assert [r[0] for r in res.rows] == list(range(21, 50))


def test_planner_tightens_multiple_upper_bounds():
    db = make_db(50)
    (res,) = db.execute_sql("SELECT id FROM t WHERE id < 30 AND id <= 20")
    assert db.last_scan_used_index is True
    assert [r[0] for r in res.rows] == list(range(21))


def test_planner_contradictory_equalities_return_nothing():
    db = make_db(50)
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 5 AND id = 6")
    assert res.rows == []
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 5 AND id = 5")
    assert res.rows == [[5]]


def test_planner_eq_vs_range_contradiction_returns_nothing():
    db = make_db(50)
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 2 AND id > 3")
    assert res.rows == []
    (res,) = db.execute_sql("SELECT id FROM t WHERE id = 5 AND id > 3")
    assert res.rows == [[5]]  # consistent eq+range still returns the row


# --------------------------------------- executable oracle: planner vs scan


def test_planner_differential_random_where():
    """The anti-leniency oracle: for 600 random WHERE conjunctions, the
    index-planned pipeline must return exactly what a brute-force full scan
    of the same predicate returns. This class of test exists because
    self-review of one's own planner cannot be trusted to catch planner
    logic errors -- the two bugs this file locks in were both invisible to
    hand-written single-case tests."""
    import random

    from picosql import ast as ast_mod
    from picosql.engine import _combine_conjunction, _where_pass, eval_expr
    from picosql.executor import SeqScanOperator

    rng = random.Random(99)
    db = make_db(120)
    table = db.tables["t"]
    ops = ["=", "!=", "<", "<=", ">", ">="]

    for _trial in range(600):
        factors = []
        for _ in range(rng.randint(1, 3)):
            op = rng.choice(ops)
            value = rng.randint(0, 130)
            col = ast_mod.ColumnRef("id")
            lit = ast_mod.Literal(value)
            if rng.random() < 0.5:
                factors.append(ast_mod.BinaryOp(op, col, lit))
            else:  # mirrored form
                factors.append(ast_mod.BinaryOp(op, lit, col))
        if rng.random() < 0.3:
            factors.append(
                ast_mod.BinaryOp(
                    "=", ast_mod.ColumnRef("grp"), ast_mod.Literal(rng.randint(0, 12))
                )
            )

        # ground truth: brute-force evaluation of the whole conjunction
        full_expr = _combine_conjunction(factors)
        expected = sorted(
            row[0]
            for _, row in table.store.scan()
            if _where_pass(eval_expr(full_expr, row, table))
        )

        # actual: the planner's index pipeline, built exactly the way
        # _execute_select builds it
        plan, residual_factors = _extract_pk_plan(table, factors)
        scan = plan(table) if plan else SeqScanOperator(table)
        residual = _combine_conjunction(residual_factors)
        from picosql.executor import FilterOperator

        op_pipe = scan
        if residual is not None:
            op_pipe = FilterOperator(
                op_pipe, lambda row: _where_pass(eval_expr(residual, row, table))
            )
        actual = sorted(row[0] for row in op_pipe.drain())

        assert actual == expected, (
            f"planner mismatch for factors {[f.op for f in factors]}: "
            f"{actual[:10]}... != {expected[:10]}..."
        )
