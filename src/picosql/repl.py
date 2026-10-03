"""Interactive REPL: ``python -m picosql``.

The loop is written with injectable ``read_line`` / ``echo`` callables so
tests can drive it without a real terminal.
"""

from __future__ import annotations

from typing import Callable, Optional

from .engine import Database, EngineError, ExecuteResult, QueryResult
from .lexer import SqlError
from .parser import parse

BANNER = "pico-sql v1.0.0 -- a tiny SQL engine for learning (.help for help)"

HELP = (
    ".quit              exit (works even with a pending statement)\n"
    ".tables            list tables\n"
    "CREATE / INSERT / SELECT / UPDATE / DELETE ... ;   run SQL (end with ';')\n"
    "a string literal may span lines -- the REPL keeps reading until quotes balance"
)


def _statement_incomplete(buffer: str) -> bool:
    """True while the buffer sits inside an open string literal.

    Matches the lexer's string rule: ``''`` escapes a quote, so an ODD number
    of single quotes means a string is still open. This is what lets a
    multi-line string containing ``;`` or even ``.quit`` survive the REPL's
    statement-splitting heuristic.
    """
    return buffer.count("'") % 2 == 1


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
    banner: str = BANNER,
) -> Database:
    """Run the REPL. Returns the database so tests can inspect the state."""
    db = database if database is not None else Database()
    echo(banner)
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
        incomplete = _statement_incomplete(buffer)

        # meta commands are recognized outside string literals; .quit/.exit
        # always work so a pending statement can never trap the user
        if not incomplete and stripped.startswith("."):
            if stripped in (".quit", ".exit"):
                break
            if buffer:
                echo(
                    "cannot run meta commands while a statement is pending "
                    "(end it with ';' or abort with .quit)"
                )
                continue
            if stripped == ".help":
                echo(HELP)
            elif stripped == ".tables":
                names = ", ".join(db.tables) if db.tables else "(no tables)"
                echo(names)
            else:
                echo(f"unknown command {stripped!r}")
            continue

        buffer += ("\n" if buffer else "") + line
        if buffer.rstrip().endswith(";") and not _statement_incomplete(buffer):
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
    import argparse

    parser = argparse.ArgumentParser(
        prog="pico-sql",
        description="A tiny SQL engine for learning how databases work.",
    )
    parser.add_argument(
        "file",
        nargs="?",
        default=None,
        help="database file (omit to run against in-memory pages)",
    )
    parser.add_argument(
        "--buffer-pages",
        type=int,
        default=64,
        help="buffer pool capacity in pages (default: 64)",
    )
    args = parser.parse_args()

    if args.file is not None:
        db = Database(args.file, buffer_capacity=args.buffer_pages)
        banner = f"{BANNER} [db: {args.file}]"
    else:
        db = Database(buffer_capacity=args.buffer_pages)
        banner = f"{BANNER} [in-memory: pass a file path to persist]"
    try:
        run_loop(database=db, banner=banner)
    finally:
        db.close()
