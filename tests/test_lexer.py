import pytest

from picosql.lexer import SqlError, tokenize


def test_keywords_are_uppercased_and_identifiers_keep_case():
    tokens = tokenize("select Name from USERS")
    types = [(t.type, t.value) for t in tokens]
    assert types == [
        ("KEYWORD", "SELECT"),
        ("IDENT", "Name"),
        ("KEYWORD", "FROM"),
        ("IDENT", "USERS"),
        ("EOF", None),
    ]


def test_numbers_int_and_float():
    tokens = tokenize("1 2.5 007")
    values = [t.value for t in tokens if t.type == "NUMBER"]
    assert values == [1, 2.5, 7]
    assert isinstance(values[0], int)
    assert isinstance(values[1], float)


def test_string_with_escaped_quote():
    tokens = tokenize("'it''s ok'")
    assert tokens[0].type == "STRING"
    assert tokens[0].value == "it's ok"


def test_line_comment_is_skipped():
    tokens = tokenize("SELECT 1 -- the rest is a comment\n, 2")
    values = [t.value for t in tokens if t.type in ("NUMBER", "PUNCT")]
    assert values == [1, ",", 2]


def test_two_char_operators_match_before_single():
    tokens = tokenize("a <= b != c <> d")
    ops = [t.value for t in tokens if t.type == "OP"]
    assert ops == ["<=", "!=", "<>"]


def test_unterminated_string_raises_with_position():
    with pytest.raises(SqlError) as excinfo:
        tokenize("SELECT 'oops")
    assert "unterminated" in str(excinfo.value)


def test_unexpected_character_raises():
    with pytest.raises(SqlError) as excinfo:
        tokenize("SELECT 1 # 2")
    assert "#" in str(excinfo.value)


def test_tokens_carry_line_and_col():
    tokens = tokenize("SELECT\n  name")
    name_token = tokens[1]
    assert name_token.line == 2
