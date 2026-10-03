"""pico-sql: a tiny relational database engine in pure Python, for learning.

Pipeline: text --tokenize--> tokens --parse--> AST --Database.execute--> results
"""

from .engine import Database, EngineError, ExecuteResult, QueryResult
from .lexer import SqlError, Token, tokenize
from .parser import parse

__version__ = "1.0.0"

__all__ = [
    "Database",
    "EngineError",
    "ExecuteResult",
    "QueryResult",
    "SqlError",
    "Token",
    "parse",
    "tokenize",
    "__version__",
]
