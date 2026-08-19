"""
gsheet_connector.py
--------------------
Lets a client/admin plug in a Google Sheet as a LIVE data source on the
Connect Data page, same spirit as db_connector.py.

Two modes, both surfaced in the UI so people can pick whichever fits:

1. PUBLIC LINK  - the sheet is shared "Anyone with the link -> Viewer".
   We turn the normal share URL into Google's CSV export URL and read it
   straight into pandas. No credentials, no extra library beyond `requests`
   (which the app already depends on for the AI Assistant page).

2. PRIVATE / SERVICE ACCOUNT - for sheets that must stay private. Needs a
   Google Cloud service-account JSON key (pasted/uploaded by the user) and
   the `gspread` + `google-auth` packages. The sheet must be shared with the
   service account's client_email like any other collaborator.

SAFETY / PRIVACY, same philosophy as db_connector.py: nothing here is ever
written to disk. A pasted service-account key lives only in
st.session_state for the current browser session (see workspace_store.py -
saved workspaces never include it), exactly like a database password.
"""

import io
import re
import json
import time

import pandas as pd
import requests
import streamlit as st

try:
    import gspread
    from google.oauth2.service_account import Credentials as _GCredentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

MAX_ROWS = 20000


class ConnectionError(Exception):
    pass


class QueryError(Exception):
    pass


# --------------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------------
def extract_sheet_id(url_or_id: str) -> str:
    """Pulls the spreadsheet ID out of a full Google Sheets URL, or just
    returns the input unchanged if it already looks like a bare ID."""
    text = (url_or_id or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    if m:
        return m.group(1)
    return text


def extract_gid(url_or_id: str, default_gid: str = "0") -> str:
    """Pulls the worksheet (tab) gid out of a URL like '...#gid=123456',
    falling back to the first tab (gid=0) when the URL doesn't carry one."""
    text = (url_or_id or "").strip()
    m = re.search(r"[#&]gid=(\d+)", text)
    return m.group(1) if m else default_gid


# --------------------------------------------------------------------------------
# MODE 1 - Public link (CSV export)
# --------------------------------------------------------------------------------
def public_csv_url(url_or_id: str, gid: str = "0") -> str:
    sheet_id = extract_sheet_id(url_or_id)
    if not sheet_id:
        raise ConnectionError("Paste a Google Sheet link or ID first.")
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"


def load_public_sheet(url_or_id: str, gid: str = "0", row_limit: int = MAX_ROWS) -> pd.DataFrame:
    """Reads a publicly-shared ('Anyone with the link') Google Sheet tab as a
    DataFrame. Raises ConnectionError with a friendly message if the sheet
    isn't actually public, doesn't exist, or the link is malformed."""
    csv_url = public_csv_url(url_or_id, gid)
    try:
        resp = requests.get(csv_url, timeout=20)
    except requests.RequestException as e:
        raise ConnectionError(f"Could not reach Google Sheets: {e}")

    if resp.status_code == 404:
        raise ConnectionError("Sheet not found — check the link is correct.")
    if resp.status_code in (401, 403) or "<html" in resp.text[:200].lower():
        raise ConnectionError(
            "Couldn't read this sheet as public CSV. Make sure it's shared as "
            "'Anyone with the link → Viewer' (Share button, top-right of the sheet), "
            "or use the Private / Service Account option instead for sheets that "
            "must stay restricted."
        )
    if resp.status_code != 200:
        raise ConnectionError(f"Google Sheets returned an error (HTTP {resp.status_code}).")

    try:
        df = pd.read_csv(io.StringIO(resp.text))
    except Exception as e:
        raise ConnectionError(f"Sheet reached but couldn't be parsed as a table: {e}")

    if df.empty:
        raise ConnectionError("That sheet/tab has no rows.")
    if len(df) > row_limit:
        df = df.head(row_limit)
    return df


def list_public_tabs_hint() -> str:
    return ("Tip: to load a specific tab (not the first one), open that tab in your "
            "browser and copy the '#gid=...' number from the URL into the field below.")


# --------------------------------------------------------------------------------
# MODE 2 - Private sheet via service account (gspread)
# --------------------------------------------------------------------------------
def parse_service_account_json(raw_text: str) -> dict:
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise ConnectionError("Paste or upload the service account JSON key first.")
    try:
        info = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ConnectionError(f"That doesn't look like valid JSON: {e}")
    if info.get("type") != "service_account" or "client_email" not in info:
        raise ConnectionError("That JSON doesn't look like a Google service-account key "
                               "(expected a 'type': 'service_account' field and a 'client_email').")
    return info


def get_client(service_account_info: dict):
    """Builds an authorized gspread client from a parsed service-account dict.
    Cached per-account (by client_email) for the life of the process."""
    if not GSPREAD_AVAILABLE:
        raise ConnectionError(
            "gspread isn't installed in this environment yet. Add 'gspread' and "
            "'google-auth' to requirements.txt and reinstall."
        )
    return _get_client_cached(json.dumps(service_account_info, sort_keys=True))


@st.cache_resource(show_spinner=False)
def _get_client_cached(service_account_json_str: str):
    info = json.loads(service_account_json_str)
    creds = _GCredentials.from_service_account_info(info, scopes=_SCOPES)
    return gspread.authorize(creds)


def test_connection(client, sheet_id: str):
    """Opens the spreadsheet to confirm the service account can see it.
    Raises ConnectionError with a readable message on failure."""
    try:
        sh = client.open_by_key(extract_sheet_id(sheet_id))
        return sh
    except gspread.exceptions.SpreadsheetNotFound:
        raise ConnectionError(
            "Spreadsheet not found or not shared with this service account. Share the "
            "sheet (Share button) with the service account's client_email as a Viewer."
        )
    except Exception as e:
        raise ConnectionError(str(e))


def list_worksheets(client, sheet_id: str):
    """Best-effort list of tab names, for a helpful picker. Returns [] on any
    failure rather than raising - this is a convenience, not a required step."""
    try:
        sh = client.open_by_key(extract_sheet_id(sheet_id))
        return [ws.title for ws in sh.worksheets()]
    except Exception:
        return []


def load_private_worksheet(client, sheet_id: str, worksheet_name: str, row_limit: int = MAX_ROWS) -> pd.DataFrame:
    try:
        sh = client.open_by_key(extract_sheet_id(sheet_id))
        ws = sh.worksheet(worksheet_name) if worksheet_name else sh.sheet1
        records = ws.get_all_records()
    except gspread.exceptions.SpreadsheetNotFound:
        raise QueryError("Spreadsheet not found or not shared with this service account.")
    except gspread.exceptions.WorksheetNotFound:
        raise QueryError(f"Tab '{worksheet_name}' not found in this sheet.")
    except Exception as e:
        raise QueryError(str(e))

    df = pd.DataFrame(records)
    if df.empty:
        raise QueryError("That tab has no rows (or the first row isn't a proper header row).")
    if len(df) > row_limit:
        df = df.head(row_limit)
    return df
