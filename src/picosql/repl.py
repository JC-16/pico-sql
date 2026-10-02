"""Interactive REPL: ``python -m picosql``.

The loop is written with injectable ``read_line`` / ``echo`` callables so
tests can drive it without a real terminal.
"""

from __future__ import annotations

from typing import Callable, Optional

from .engine import Database, EngineError, ExecuteResult, QueryResult
from .lexer import SqlError
from .parser import parse

BANNER = "pico-sql v0.1.0 -- a tiny SQL engine for learning (.help for help)"

HELP = (
    ".quit              exit\n"
    ".tables            list tables\n"
    "CREATE / INSERT / SELECT / UPDATE / DELETE ... ;   run SQL (end with ';')"
)


def format_value(value) -> str:
    if value is None:
        return "NULL"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def format_table(columns: list, rows: list) -> str:
    """Render a QueryResult as an ASCII table (+---+ style)."""
    cells = [[format_value(v) for v in row] for row in rows]
    widths = []
    for i, name in enumerate(columns):
        width = len(name)
        for row in cells:
            width = max(width, len(row[i]))
        widths.append(width)

    def render_row(values: list) -> str:
        return "|" + "|".join(f" {v.ljust(widths[i])} " for i, v in enumerate(values)) + "|"

    separator = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [separator, render_row(columns), separator]
    lines.extend(render_row(row) for row in cells)
    lines.append(separator)
    lines.append(f"{len(rows)} row(s)")
    return "\n".join(lines)


def run_loop(
    read_line: Callable[[str], Optional[str]] = input,
    echo: Callable[[str], None] = print,
    database: Optional[Database] = None,
) -> Database:
    """Run the REPL. Returns the database so tests can inspect the state."""
    db = database if database is not None else Database()
    echo(BANNER)
    buffer = ""
    while True:
        prompt = "pico-sql> " if not buffer else "       -> "
        try:
            line = read_line(prompt)
        except (EOFError, StopIteration):
            break
        if line is None:
            break
        stripped = line.strip()

        if not buffer and stripped.startswith("."):
            if stripped in (".quit", ".exit"):
                break
            if stripped == ".help":
                echo(HELP)
            elif stripped == ".tables":
                names = ", ".join(db.tables) if db.tables else "(no tables)"
                echo(names)
            else:
                echo(f"unknown command {stripped!r}")
            continue

        buffer += ("\n" if buffer else "") + line
        if not buffer.rstrip().endswith(";"):
            continue
        try:
            for stmt in parse(buffer):
                result = db.execute(stmt)
                if isinstance(result, QueryResult):
                    echo(format_table(result.columns, result.rows))
                elif isinstance(result, ExecuteResult):
                    echo(result.message)
                else:  # pragma: no cover - defensive
                    echo(repr(result))
        except (SqlError, EngineError) as exc:
            echo(f"ERROR: {exc}")
        buffer = ""
    return db


def main() -> None:
    run_loop()
