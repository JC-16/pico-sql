"""Recursive-descent SQL parser: token stream -> AST.

Grammar (v1 subset, see docs/design.md for the full EBNF):

    statement     := create_table | drop_table | insert | select
                   | update | delete
    create_table  := CREATE TABLE ident ( column_def (, column_def)* )
    column_def    := ident type [PRIMARY KEY]
    insert        := INSERT INTO ident [( ident (, ident)* )] VALUES row (, row)*
    row           := ( expr (, expr)* )
    select        := SELECT (* | ident (, ident)*) FROM ident
                     [WHERE expr] [ORDER BY ident [ASC|DESC]] [LIMIT number]
    update        := UPDATE ident SET ident = expr (, ident = expr)* [WHERE expr]
    delete        := DELETE FROM ident [WHERE expr]

Expressions are layered by precedence, one layer per method -- that is the
core trick of recursive descent:

    parse_or -> parse_and -> parse_not -> parse_comparison
             -> parse_additive -> parse_multiplicative -> parse_unary -> parse_primary
"""

from __future__ import annotations

from . import ast
from .lexer import SqlError, Token, tokenize

_COMPARISON_OPS = ("=", "!=", "<>", "<", "<=", ">", ">=")


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    # ------------------------------------------------------------- primitives

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def next(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def at_kw(self, *keywords: str) -> bool:
        tok = self.peek()
        return tok.type == "KEYWORD" and tok.value in keywords

    def eat_kw(self, keyword: str) -> Token:
        if not self.at_kw(keyword):
            tok = self.peek()
            raise SqlError(f"expected {keyword}, got {tok.value!r}", tok.line, tok.col)
        return self.next()

    def expect_punct(self, ch: str) -> Token:
        tok = self.peek()
        if tok.type != "PUNCT" or tok.value != ch:
            raise SqlError(f"expected {ch!r}, got {tok.value!r}", tok.line, tok.col)
        return self.next()

    def eat_punct_opt(self, ch: str) -> bool:
        tok = self.peek()
        if tok.type == "PUNCT" and tok.value == ch:
            self.next()
            return True
        return False

    def expect_ident(self) -> str:
        tok = self.peek()
        if tok.type != "IDENT":
            raise SqlError(f"expected identifier, got {tok.value!r}", tok.line, tok.col)
        return self.next().value

    def expect_int(self, what: str) -> int:
        tok = self.next()
        if tok.type != "NUMBER" or not isinstance(tok.value, int):
            raise SqlError(f"{what} must be an integer", tok.line, tok.col)
        return tok.value

    # ----------------------------------------------------------------- driver

    def parse_statements(self) -> list:
        """Parse a semicolon-separated script. Returns a list of statements.

        Empty statements are tolerated, as in real SQL: leading, trailing
        and repeated semicolons (;;...;) parse to nothing.
        """
        statements = []
        while True:
            while self.eat_punct_opt(";"):
                pass
            if self.peek().type == "EOF":
                break
            statements.append(self.parse_statement())
            tok = self.peek()
            if tok.type == "EOF":
                break
            if not self.eat_punct_opt(";"):
                raise SqlError(
                    f"expected ';' after statement, got {tok.value!r}", tok.line, tok.col
                )
        return statements

    def parse_statement(self):
        if self.at_kw("CREATE"):
            return self.parse_create()
        if self.at_kw("DROP"):
            return self.parse_drop()
        if self.at_kw("INSERT"):
            return self.parse_insert()
        if self.at_kw("SELECT"):
            return self.parse_select()
        if self.at_kw("UPDATE"):
            return self.parse_update()
        if self.at_kw("DELETE"):
            return self.parse_delete()
        tok = self.peek()
        raise SqlError(
            f"unsupported statement starting with {tok.value!r}", tok.line, tok.col
        )

    # -------------------------------------------------------------- DDL / DML

    def parse_create(self) -> ast.CreateTable:
        self.eat_kw("CREATE")
        self.eat_kw("TABLE")
        name = self.expect_ident()
        self.expect_punct("(")
        columns = [self.parse_column_def()]
        while self.eat_punct_opt(","):
            columns.append(self.parse_column_def())
        self.expect_punct(")")
        names = [c.name for c in columns]
        if len(set(names)) != len(names):
            raise SqlError(f"duplicate column name in table {name!r}", 0, 0)
        if sum(1 for c in columns if c.is_primary) > 1:
            raise SqlError("multiple PRIMARY KEY columns are not allowed", 0, 0)
        return ast.CreateTable(name, columns)

    def parse_column_def(self) -> ast.ColumnDef:
        name = self.expect_ident()
        tok = self.peek()
        if tok.type != "KEYWORD" or tok.value not in ("INT", "FLOAT", "VARCHAR", "BOOL"):
            raise SqlError(f"expected column type, got {tok.value!r}", tok.line, tok.col)
        self.next()
        varchar_len = None
        if tok.value == "VARCHAR" and self.eat_punct_opt("("):
            varchar_len = self.expect_int("VARCHAR length")
            self.expect_punct(")")
        is_primary = False
        if self.at_kw("PRIMARY"):
            self.next()
            self.eat_kw("KEY")
            is_primary = True
        return ast.ColumnDef(name, tok.value, varchar_len, is_primary)

    def parse_drop(self) -> ast.DropTable:
        self.eat_kw("DROP")
        self.eat_kw("TABLE")
        return ast.DropTable(self.expect_ident())

    def parse_insert(self) -> ast.Insert:
        self.eat_kw("INSERT")
        self.eat_kw("INTO")
        table = self.expect_ident()
        columns = None
        if self.peek().type == "PUNCT" and self.peek().value == "(":
            # After the table name, '(' can only start a column list:
            # the VALUES keyword cannot appear inside parentheses here.
            self.next()
            columns = [self.expect_ident()]
            while self.eat_punct_opt(","):
                columns.append(self.expect_ident())
            self.expect_punct(")")
            if len(set(columns)) != len(columns):
                raise SqlError(f"duplicate column in INSERT INTO {table!r}", 0, 0)
        self.eat_kw("VALUES")
        rows = [self.parse_value_row()]
        while self.eat_punct_opt(","):
            rows.append(self.parse_value_row())
        return ast.Insert(table, columns, rows)

    def parse_value_row(self) -> list:
        self.expect_punct("(")
        exprs = [self.parse_expr()]
        while self.eat_punct_opt(","):
            exprs.append(self.parse_expr())
        self.expect_punct(")")
        return exprs

    def parse_select(self) -> ast.Select:
        self.eat_kw("SELECT")
        tok = self.peek()
        if tok.type == "OP" and tok.value == "*":
            self.next()
            columns = ["*"]
        else:
            columns = [self.expect_ident()]
            while self.eat_punct_opt(","):
                columns.append(self.expect_ident())
        self.eat_kw("FROM")
        table = self.expect_ident()
        where = self.parse_where_opt()
        order_by = None
        if self.at_kw("ORDER"):
            self.next()
            self.eat_kw("BY")
            col = self.expect_ident()
            desc = False
            if self.at_kw("ASC"):
                self.next()
            elif self.at_kw("DESC"):
                self.next()
                desc = True
            order_by = (col, desc)
        limit = None
        if self.at_kw("LIMIT"):
            self.next()
            limit = self.expect_int("LIMIT")
            if limit < 0:
                tok = self.tokens[self.pos - 1]
                raise SqlError("LIMIT must be non-negative", tok.line, tok.col)
        return ast.Select(table, columns, where, order_by, limit)

    def parse_update(self) -> ast.Update:
        self.eat_kw("UPDATE")
        table = self.expect_ident()
        self.eat_kw("SET")
        assignments = []
        while True:
            col = self.expect_ident()
            self._eat_op("=")
            assignments.append((col, self.parse_expr()))
            if not self.eat_punct_opt(","):
                break
        where = self.parse_where_opt()
        return ast.Update(table, assignments, where)

    def _eat_op(self, op: str) -> Token:
        tok = self.peek()
        if tok.type != "OP" or tok.value != op:
            raise SqlError(f"expected {op!r}, got {tok.value!r}", tok.line, tok.col)
        return self.next()

    def parse_delete(self) -> ast.Delete:
        self.eat_kw("DELETE")
        self.eat_kw("FROM")
        table = self.expect_ident()
        return ast.Delete(table, self.parse_where_opt())

    def parse_where_opt(self):
        if self.at_kw("WHERE"):
            self.next()
            return self.parse_expr()
        return None

    # ------------------------------------------------------------ expressions
    # Each precedence level gets its own method; tighter bindings bind deeper.

    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while self.at_kw("OR"):
            self.next()
            left = ast.BinaryOp("OR", left, self.parse_and())
        return left

    def parse_and(self):
        left = self.parse_not()
        while self.at_kw("AND"):
            self.next()
            left = ast.BinaryOp("AND", left, self.parse_not())
        return left

    def parse_not(self):
        if self.at_kw("NOT"):
            self.next()
            return ast.UnaryOp("NOT", self.parse_not())
        return self.parse_comparison()

    def parse_comparison(self):
        left = self.parse_additive()
        tok = self.peek()
        if tok.type == "OP" and tok.value in _COMPARISON_OPS:
            self.next()
            op = "!=" if tok.value == "<>" else tok.value
            return ast.BinaryOp(op, left, self.parse_additive())
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.peek().type == "OP" and self.peek().value in ("+", "-"):
            op = self.next().value
            left = ast.BinaryOp(op, left, self.parse_multiplicative())
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.peek().type == "OP" and self.peek().value in ("*", "/", "%"):
            op = self.next().value
            left = ast.BinaryOp(op, left, self.parse_unary())
        return left

    def parse_unary(self):
        if self.peek().type == "OP" and self.peek().value == "-":
            self.next()
            return ast.UnaryOp("NEG", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok.type in ("NUMBER", "STRING"):
            self.next()
            return ast.Literal(tok.value)
        if self.at_kw("NULL"):
            self.next()
            return ast.Literal(None)
        if self.at_kw("TRUE"):
            self.next()
            return ast.Literal(True)
        if self.at_kw("FALSE"):
            self.next()
            return ast.Literal(False)
        if tok.type == "IDENT":
            self.next()
            return ast.ColumnRef(tok.value)
        if tok.type == "PUNCT" and tok.value == "(":
            self.next()
            expr = self.parse_expr()
            self.expect_punct(")")
            return expr
        raise SqlError(f"unexpected token {tok.value!r} in expression", tok.line, tok.col)


def parse(text: str) -> list:
    """Parse a SQL script (one or more ';'-separated statements) into ASTs."""
    return Parser(tokenize(text)).parse_statements()
