"""
query_engine.py
----------------
Real SQL over the currently loaded dataset, powered by DuckDB - an in-process
analytical SQL engine that can query a pandas DataFrame directly (no separate
database file or server needed). Supports SELECT, WHERE, GROUP BY, ORDER BY,
computed columns, window functions, CTEs (WITH ...) - actual SQL, not a
lookalike.

Used by the "🖥️ SQL Query" tab on the Data Table page (Phase 5). Phase 6's
Custom Reports / Pivot page is planned to build on this same engine. DuckDB
also has first-class extensions for querying a real Postgres/MySQL database
instead of a local dataframe (postgres_scanner / mysql_scanner) - so this
module is the natural place to add that later without touching the UI code
that calls it.

SAFETY: this module runs whatever text a logged-in user typed, so it is
deliberately restricted to read-only queries:
  - only ONE statement is allowed (no ';' inside the query, so no stacking
    a second statement after a legitimate SELECT)
  - the statement must start with SELECT or WITH
  - keywords that could touch the filesystem, install extensions, or change
    data are blocked outright (INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/
    ATTACH/DETACH/COPY/EXPORT/IMPORT/INSTALL/LOAD/PRAGMA/CALL, plus the
    file-reading table functions read_csv/read_parquet/read_json/glob)
  - the DuckDB connection itself has enable_external_access turned off, so
    even a query that slipped past the checks above still can't reach the
    local filesystem or network
"""

import re

import duckdb
import pandas as pd

MAX_ROWS = 20000
DEFAULT_TABLE_NAME = "data"

_BLOCKED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"install|load|pragma|call|vacuum|checkpoint|glob|read_csv(_auto)?|read_parquet|"
    r"read_json(_auto)?)\b",
    re.IGNORECASE,
)


class QueryError(Exception):
    """Raised for anything wrong with the query itself (empty, blocked
    keyword, multiple statements, or a real DuckDB syntax/execution error) -
    callers should catch this and show str(e) to the user, it's always a
    safe, human-readable message."""
    pass


def _validate(sql: str) -> str:
    stripped = (sql or "").strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise QueryError("Query is empty.")
    if ";" in stripped:
        raise QueryError("Only a single statement is allowed — remove the ';' inside the query.")
    if not re.match(r"^\s*(with|select)\b", stripped, re.IGNORECASE):
        raise QueryError("Only SELECT (or WITH ... SELECT) queries are allowed here — this is a read-only query tool.")
    if _BLOCKED_KEYWORDS.search(stripped):
        raise QueryError("That query uses a keyword that isn't allowed here (only read-only SELECT queries over the "
                          "loaded dataset are permitted — no file access, no writes).")
    return stripped


def run_sql(df: pd.DataFrame, sql: str, table_name: str = DEFAULT_TABLE_NAME, row_limit: int = MAX_ROWS) -> pd.DataFrame:
    """Runs a single read-only SELECT over `df` (registered under
    `table_name`, default 'data') and returns the result as a new DataFrame,
    capped at `row_limit` rows. Raises QueryError on anything invalid -
    never raises a raw duckdb/internal exception to callers."""
    stripped = _validate(sql)
    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET enable_external_access=false")
        con.register(table_name, df)
        result = con.execute(stripped).df()
    except duckdb.Error as e:
        raise QueryError(str(e))
    except QueryError:
        raise
    except Exception as e:  # belt-and-braces: never leak an unhandled error to the UI
        raise QueryError(f"Could not run that query: {e}")
    finally:
        con.close()
    if len(result) > row_limit:
        result = result.head(row_limit)
    return result
