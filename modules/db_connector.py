"""
db_connector.py
----------------
Generic SQL database connector - lets a client/admin plug in ANY database
SQLAlchemy can talk to (PostgreSQL, MySQL/MariaDB, SQLite, SQL Server,
Oracle, ...) and run SELECT queries against it from the Data Table page,
independent of whatever file-based dataset is loaded on Raw Analysis.

Nothing about connection credentials is ever written to disk by this module
or by workspace_store.py - connection details live only in st.session_state
for the current browser session, so a saved workspace file never leaks a
database password.

SAFETY: same read-only philosophy as query_engine.py - only a single
SELECT/WITH statement is allowed; obviously destructive keywords are
rejected before anything is sent to the database.
"""

import json
import re
import time

import pandas as pd
import streamlit as st

try:
    import sqlalchemy as sa
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

MAX_ROWS = 20000

_BLOCKED_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|exec|execute|call)\b",
    re.IGNORECASE,
)

# Friendly presets for building a connection URI from separate fields,
# instead of forcing everyone to memorise SQLAlchemy driver strings.
DB_TYPES = {
    "PostgreSQL": {"driver": "postgresql+psycopg2", "default_port": 5432},
    "MySQL / MariaDB": {"driver": "mysql+pymysql", "default_port": 3306},
    "SQL Server": {"driver": "mssql+pyodbc", "default_port": 1433},
    "SQLite (local file path)": {"driver": "sqlite", "default_port": None},
    "Custom SQLAlchemy URI": {"driver": None, "default_port": None},
}


class ConnectionError(Exception):
    pass


class QueryError(Exception):
    pass


def build_uri(db_type: str, host="", port="", database="", username="", password="", raw_uri="", odbc_driver=""):
    """Builds a SQLAlchemy connection URI from friendly fields (or just
    returns the user-typed raw URI for 'Custom SQLAlchemy URI' / SQLite)."""
    info = DB_TYPES.get(db_type, {})
    if db_type == "Custom SQLAlchemy URI" or not info.get("driver"):
        return raw_uri.strip()
    if db_type == "SQLite (local file path)":
        path = database.strip() or raw_uri.strip()
        return f"sqlite:///{path}"

    driver = info["driver"]
    port = port or info.get("default_port") or ""
    auth = ""
    if username:
        from urllib.parse import quote_plus
        auth = quote_plus(username)
        if password:
            auth += f":{quote_plus(password)}"
        auth += "@"
    uri = f"{driver}://{auth}{host}"
    if port:
        uri += f":{port}"
    if database:
        uri += f"/{database}"
    if db_type == "SQL Server" and odbc_driver:
        uri += f"?driver={odbc_driver.replace(' ', '+')}"
    return uri


@st.cache_resource(show_spinner=False)
def _get_engine(uri: str):
    return sa.create_engine(uri, pool_pre_ping=True, pool_recycle=280)


def get_engine(uri: str):
    if not SQLALCHEMY_AVAILABLE:
        raise ConnectionError(
            "SQLAlchemy isn't installed in this environment. Add 'sqlalchemy' (plus a driver "
            "like psycopg2-binary / pymysql for your database) to requirements.txt and reinstall."
        )
    if not uri:
        raise ConnectionError("Connection string is empty.")
    return _get_engine(uri)


def test_connection(uri: str):
    """Raises ConnectionError with a readable message on failure; returns True on success."""
    try:
        engine = get_engine(uri)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
        return True
    except ConnectionError:
        raise
    except Exception as e:
        raise ConnectionError(str(e))


def list_tables(uri: str):
    """Best-effort list of table names, for a helpful picker. Returns [] on any failure
    rather than raising - this is a convenience, not a required step."""
    try:
        engine = get_engine(uri)
        inspector = sa.inspect(engine)
        return sorted(inspector.get_table_names())
    except Exception:
        return []


def _validate(sql: str) -> str:
    stripped = (sql or "").strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise QueryError("Query is empty.")
    if ";" in stripped:
        raise QueryError("Only a single statement is allowed — remove the ';' inside the query.")
    if not re.match(r"^\s*(with|select)\b", stripped, re.IGNORECASE):
        raise QueryError("Only SELECT (or WITH ... SELECT) queries are allowed here — this is a read-only connector.")
    if _BLOCKED_KEYWORDS.search(stripped):
        raise QueryError("That query uses a keyword that isn't allowed here — only read-only SELECT queries are permitted.")
    return stripped


def _stringify_unhashable_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Postgres JSON/JSONB columns (e.g. an 'extra_data' column) come back
    from SQLAlchemy as native Python dict/list objects, not strings.

    That breaks almost everything downstream: pandas' .nunique(), groupby,
    sort_values, drop_duplicates, and every filter widget in this app all
    need to hash each value, and dict/list are unhashable in Python - so a
    JSONB column crashes with "TypeError: unhashable type: 'dict'" the
    moment the app tries to profile or analyze it (as opposed to a CSV/Excel
    load, where everything already arrives as plain text).

    Fix: convert any column that contains dict/list values into its JSON
    text representation right when the query result is read, so the rest of
    the app can treat it exactly like any other text column.
    """
    for col in df.columns:
        if df[col].dtype == object:
            sample = df[col].dropna()
            if not sample.empty and sample.map(lambda v: isinstance(v, (dict, list))).any():
                df[col] = df[col].map(
                    lambda v: json.dumps(v, default=str, ensure_ascii=False)
                    if isinstance(v, (dict, list)) else v
                )
    return df


def run_query(uri: str, sql: str, row_limit: int = MAX_ROWS) -> pd.DataFrame:
    """Runs a single read-only SELECT against the external database and
    returns the result as a DataFrame, capped at row_limit rows."""
    stripped = _validate(sql)
    engine = get_engine(uri)
    try:
        with engine.connect() as conn:
            result = conn.execute(sa.text(stripped))
            rows = result.fetchmany(row_limit + 1)
            cols = list(result.keys())
        df = pd.DataFrame(rows, columns=cols)
        df = _stringify_unhashable_columns(df)
        if len(df) > row_limit:
            df = df.head(row_limit)
        return df
    except QueryError:
        raise
    except Exception as e:
        raise QueryError(str(e))


def new_query_tab(name="Query 1", sql="SELECT 1"):
    return {
        "id": f"q_{int(time.time() * 1000) % 10_000_000}",
        "name": name,
        "sql": sql,
        "auto_refresh": False,
        "refresh_interval_sec": 30,
        "last_run_ts": 0.0,
        "result_columns": None,
        "row_count": None,
        "error": None,
    }
