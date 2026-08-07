"""
workspace_store.py
-------------------
On-disk persistence for the currently loaded dataset + Boss Dashboard layout +
Custom Builder content, so NONE of it disappears when:
  - the app.py process is stopped/restarted (server restart, redeploy, crash)
  - a different browser tab/session opens the app under the SAME account (or
    an account linked to the same workspace) - everyone sharing a workspace
    sees the same currently-loaded dataset, instead of each session starting
    empty.

Phase 4 (multi-tenant): data is no longer one single shared blob. Every call
here takes a `workspace_id` - each client account (and any admin/viewer
"viewing as" that client) gets its own folder under workspace_state/, so one
client's data is never visible to another. workspace_id comes from
auth.get_workspace_id(username).

Data is only ever removed when an admin explicitly resets it from the
Admin Panel -> "Reset Workspace Data" tab, for a specific workspace_id.
Nothing here auto-expires or auto-clears on its own.
"""

import os
import pickle
import re

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_ROOT = os.path.join(APP_DIR, "workspace_state")

# Everything listed here is saved to disk after every run and restored into a
# brand-new session automatically. Keep this in sync with app.py's init_state().
PERSISTED_KEYS = [
    "df_raw",
    "meta",
    "data_source_name",
    "filters",
    "dashboard_charts",
    "pinned_kpis",
    "custom_kpis",
    "custom_charts",
    "dashboard_slicers",
    "dashboard_name",
    "pivot_reports",
]
# NOTE: db_queries / db_conn_uri (external Database Connector state) are
# INTENTIONALLY excluded from persistence - a database connection string
# can contain a plaintext password, and we never want that written to disk
# inside the pickled workspace file. Each browser session must (re)connect.

_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9_.\-]")


def _migrate_legacy_flat_file():
    """Pre-Phase-4 installs kept a single flat workspace_state/current.pkl
    (one shared dataset for everyone). Move it into the 'admin' workspace
    folder so it isn't silently lost on upgrade - the admin can then view
    it (View as: My own data) and decide whether to keep it or reassign it
    to a specific client via the Admin Panel. Runs once, harmlessly, at
    import time; no-ops if there's nothing to migrate or migration already
    happened."""
    legacy_path = os.path.join(STORE_ROOT, "current.pkl")
    if not os.path.isfile(legacy_path):
        return
    try:
        target_dir = os.path.join(STORE_ROOT, "admin")
        target_path = os.path.join(target_dir, "current.pkl")
        if not os.path.exists(target_path):
            os.makedirs(target_dir, exist_ok=True)
            os.replace(legacy_path, target_path)
        else:
            os.remove(legacy_path)
    except Exception:
        pass


_migrate_legacy_flat_file()


def _safe_dir(workspace_id: str) -> str:
    """Turns a workspace_id (== some account's username, chosen by an admin)
    into a filesystem-safe folder name. Never trust it to already be safe -
    usernames are admin-entered free text."""
    safe = _SAFE_ID_RE.sub("_", (workspace_id or "default").strip()) or "default"
    return os.path.join(STORE_ROOT, safe)


def _store_file(workspace_id: str) -> str:
    return os.path.join(_safe_dir(workspace_id), "current.pkl")


def save(session_state, workspace_id: str) -> None:
    """Best-effort save. Never raises - a failed save should never crash the app,
    it should just mean the next successful save wins."""
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        payload = {k: session_state.get(k) for k in PERSISTED_KEYS}
        store_file = _store_file(workspace_id)
        tmp_path = store_file + ".tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump(payload, f)
        os.replace(tmp_path, store_file)  # atomic on POSIX - no half-written files
    except Exception:
        pass


def load(workspace_id: str):
    """Returns the saved {key: value} dict for this workspace, or None if
    nothing has been saved yet (or the file is unreadable/corrupted, in
    which case we treat it as empty rather than crashing the app)."""
    store_file = _store_file(workspace_id)
    if not os.path.exists(store_file):
        return None
    try:
        with open(store_file, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def clear(workspace_id: str) -> None:
    """Deletes the saved workspace from disk. Only call this from an explicit,
    admin-confirmed reset action - never automatically."""
    try:
        store_file = _store_file(workspace_id)
        if os.path.exists(store_file):
            os.remove(store_file)
    except Exception:
        pass


def has_saved_data(workspace_id: str) -> bool:
    return os.path.exists(_store_file(workspace_id))


def list_workspace_ids():
    """Every workspace_id that currently has data saved on disk (regardless
    of whether an account still points at it) - useful for admin cleanup."""
    if not os.path.isdir(STORE_ROOT):
        return []
    return sorted(
        name for name in os.listdir(STORE_ROOT)
        if os.path.isfile(os.path.join(STORE_ROOT, name, "current.pkl"))
    )


# ----------------------------------------------------------------------------
# App branding (sidebar title) - GLOBAL, not per-workspace. Every account sees
# the same brand text/style, so it's admin-only and lives in its own file
# instead of PERSISTED_KEYS (which is per-workspace_id).
# ----------------------------------------------------------------------------
_BRAND_FILE = os.path.join(STORE_ROOT, "_branding.pkl")


def save_branding(brand: dict) -> None:
    try:
        os.makedirs(STORE_ROOT, exist_ok=True)
        tmp_path = _BRAND_FILE + ".tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump(brand, f)
        os.replace(tmp_path, _BRAND_FILE)
    except Exception:
        pass


def load_branding():
    """Returns the saved brand dict, or None if nothing's been saved yet /
    the file is unreadable (treated as 'use defaults', never crashes)."""
    if not os.path.isfile(_BRAND_FILE):
        return None
    try:
        with open(_BRAND_FILE, "rb") as f:
            return pickle.load(f)
    except Exception:
        return None
