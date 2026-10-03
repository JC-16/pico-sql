import pytest

from picosql import ast
from picosql.lexer import SqlError
from picosql.parser import parse


def test_create_table_shape():
    (stmt,) = parse("CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(10), score FLOAT)")
    assert isinstance(stmt, ast.CreateTable)
    assert stmt.table == "users"
    assert [c.name for c in stmt.columns] == ["id", "name", "score"]
    assert stmt.columns[0].is_primary is True
    assert stmt.columns[1].varchar_len == 10
    assert stmt.columns[1].is_primary is False


def test_insert_multi_row_with_column_list():
    (stmt,) = parse("INSERT INTO users (id, name) VALUES (1, 'a'), (2, 'b')")
    assert isinstance(stmt, ast.Insert)
    assert stmt.columns == ["id", "name"]
    assert len(stmt.rows) == 2
    assert stmt.rows[0][0] == ast.Literal(1)


def test_select_full_shape():
    (stmt,) = parse(
        "SELECT name, score FROM users WHERE score >= 60 "
        "ORDER BY score DESC LIMIT 3"
    )
    assert isinstance(stmt, ast.Select)
    assert stmt.columns == ["name", "score"]
    assert stmt.where == ast.BinaryOp(">=", ast.ColumnRef("score"), ast.Literal(60))
    assert stmt.order_by == ("score", True)
    assert stmt.limit == 3


def test_select_star():
    (stmt,) = parse("SELECT * FROM users")
    assert stmt.columns == ["*"]
    assert stmt.where is None


def test_and_binds_tighter_than_or():
    (stmt,) = parse("SELECT a FROM t WHERE x = 1 OR y = 2 AND z = 3")
    assert stmt.where.op == "OR"
    assert stmt.where.left == ast.BinaryOp("=", ast.ColumnRef("x"), ast.Literal(1))
    assert stmt.where.right.op == "AND"


def test_parentheses_override_precedence():
    (stmt,) = parse("SELECT a FROM t WHERE (x = 1 OR y = 2) AND z = 3")
    assert stmt.where.op == "AND"
    assert stmt.where.left.op == "OR"


def test_arithmetic_precedence():
    (stmt,) = parse("UPDATE t SET v = 1 + 2 * 3 WHERE id = 4")
    expr = stmt.assignments[0][1]
    assert expr == ast.BinaryOp(
        "+", ast.Literal(1), ast.BinaryOp("*", ast.Literal(2), ast.Literal(3))
    )


def test_unary_neg_and_not():
    (stmt,) = parse("SELECT a FROM t WHERE NOT v = -2")
    assert stmt.where.op == "NOT"
    inner = stmt.where.operand
    assert inner == ast.BinaryOp("=", ast.ColumnRef("v"), ast.UnaryOp("NEG", ast.Literal(2)))


def test_multiple_statements():
    stmts = parse("CREATE TABLE t (id INT); INSERT INTO t VALUES (1);")
    assert [type(s) for s in stmts] == [ast.CreateTable, ast.Insert]


def test_missing_semicolon_between_statements_raises():
    with pytest.raises(SqlError):
        parse("CREATE TABLE t (id INT) INSERT INTO t VALUES (1)")


def test_missing_from_raises_with_position():
    with pytest.raises(SqlError) as excinfo:
        parse("SELECT name users")
    assert "expected FROM" in str(excinfo.value)


def test_negative_limit_raises():
    with pytest.raises(SqlError):
        parse("SELECT a FROM t LIMIT -1")


def test_delete_and_update_where_optional():
    (d,) = parse("DELETE FROM t")
    assert d.where is None
    (u,) = parse("UPDATE t SET a = 1")
    assert u.where is None


def test_empty_statements_tolerated():
    # leading, repeated and trailing semicolons parse to nothing
    stmts = parse(";;CREATE TABLE t (id INT);;INSERT INTO t VALUES (1);;;;")
    assert [type(s) for s in stmts] == [ast.CreateTable, ast.Insert]
    assert parse(";") == []
    assert parse("") == []


def test_statement_without_trailing_semicolon_still_parses():
    (stmt,) = parse("SELECT a FROM t")
    assert isinstance(stmt, ast.Select)
