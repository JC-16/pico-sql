"""SQL lexer: turn raw SQL text into a stream of typed tokens.

The lexer is stage one of the pipeline:

    text --tokenize--> tokens --Parser--> AST --engine--> results

Design notes (see STUDY.md for a guided tour):
* Keywords are matched case-insensitively and normalized to UPPERCASE.
* Identifiers keep their original case.
* ``''`` inside a string literal escapes a single quote (SQL standard).
* ``--`` starts a line comment.
* Every token carries (line, col) so error messages can point at the spot.
"""

from __future__ import annotations

from dataclasses import dataclass


class SqlError(Exception):
    """A SQL syntax error with a source position (used by lexer and parser)."""

    def __init__(self, message: str, line: int, col: int):
        super().__init__(f"line {line}, col {col}: {message}")
        self.message = message
        self.line = line
        self.col = col


@dataclass(frozen=True)
class Token:
    """One lexical unit.

    type  : KEYWORD | IDENT | NUMBER | STRING | OP | PUNCT | EOF
    value : UPPERCASED keyword / identifier text / int-or-float / string / operator
    """

    type: str
    value: object
    line: int
    col: int


KEYWORDS = frozenset(
    """
    CREATE TABLE DROP INSERT INTO VALUES SELECT FROM WHERE
    ORDER BY ASC DESC LIMIT UPDATE SET DELETE AND OR NOT
    NULL TRUE FALSE PRIMARY KEY INT FLOAT VARCHAR BOOL
    """.split()
)

_TWO_CHAR_OPS = ("<=", ">=", "!=", "<>")
_ONE_CHAR_OPS = set("=<>+-*/%")
_PUNCT = set("(),;")


def tokenize(text: str) -> list[Token]:
    """Split *text* into tokens. Raises SqlError on malformed input."""
    tokens: list[Token] = []
    i, line, col = 0, 1, 1
    n = len(text)

    def advance(steps: int = 1) -> None:
        nonlocal i, line, col
        for _ in range(steps):
            if text[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    while i < n:
        ch = text[i]

        if ch in " \t\r\n":
            advance()
            continue

        if ch == "-" and text[i : i + 2] == "--":  # line comment, runs to EOL
            while i < n and text[i] != "\n":
                advance()
            continue

        start_line, start_col = line, col

        if ch == "'":  # string literal, '' escapes a quote
            advance()
            buf: list[str] = []
            while True:
                if i >= n:
                    raise SqlError("unterminated string literal", start_line, start_col)
                if text[i] == "'":
                    if text[i : i + 2] == "''":  # '' -> literal quote
                        buf.append("'")
                        advance(2)
                        continue
                    advance()
                    break
                buf.append(text[i])
                advance()
            tokens.append(Token("STRING", "".join(buf), start_line, start_col))
            continue

        if ch.isdigit():  # int or float literal
            start = i
            while i < n and text[i].isdigit():
                advance()
            is_float = False
            if i + 1 < n and text[i] == "." and text[i + 1].isdigit():
                is_float = True
                advance()
                while i < n and text[i].isdigit():
                    advance()
            raw = text[start:i]
            value = float(raw) if is_float else int(raw)
            tokens.append(Token("NUMBER", value, start_line, start_col))
            continue

        if ch.isalpha() or ch == "_":  # identifier or keyword
            start = i
            while i < n and (text[i].isalnum() or text[i] == "_"):
                advance()
            word = text[start:i]
            upper = word.upper()
            if upper in KEYWORDS:
                tokens.append(Token("KEYWORD", upper, start_line, start_col))
            else:
                tokens.append(Token("IDENT", word, start_line, start_col))
            continue

        two = text[i : i + 2]
        if two in _TWO_CHAR_OPS:  # longest match first: <= >= != <>
            tokens.append(Token("OP", two, start_line, start_col))
            advance(2)
            continue

        if ch in _ONE_CHAR_OPS:
            tokens.append(Token("OP", ch, start_line, start_col))
            advance()
            continue

        if ch in _PUNCT:
            tokens.append(Token("PUNCT", ch, start_line, start_col))
            advance()
            continue

        raise SqlError(f"unexpected character {ch!r}", line, col)

    tokens.append(Token("EOF", None, line, col))
    return tokens
