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
import time

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

# --------------------------------------------------------------------------------
# HEAVY vs LIGHT split (perf fix)
# --------------------------------------------------------------------------------
# df_raw/meta can be a large pandas DataFrame - pickling it is the expensive
# part of a save/load. It only ever changes when someone loads NEW data
# (_apply_loaded_df in app.py always REASSIGNS st.session_state.df_raw to a
# fresh object - it's never mutated in place). Everything else (pinned KPIs,
# dashboard charts, slicers, dashboard name, pivot reports, filters) is small
# and changes on almost every click (pin/unpin a KPI, add a chart, tweak a
# slicer...).
#
# Before this split, EVERY interaction anywhere in the app re-pickled the
# WHOLE dataset to disk (auto-save at the bottom of app.py), and the Boss
# Dashboard page additionally re-pickled + re-loaded the whole dataset on
# every single render just to pick up another session's pinned/unpinned
# card. For a large dataset that is exactly the "pin kiya, sab load hone me
# time lag raha" slowness that was reported - it's disk I/O on the full
# dataset, not a network issue.
#
# Now: HEAVY_KEYS go in current.pkl (written only when the data itself
# actually changes), LIGHT_KEYS go in current_light.pkl (written on every
# interaction, but cheap since it never contains the dataset).
HEAVY_KEYS = ["df_raw", "meta", "data_source_name"]
LIGHT_KEYS = [k for k in PERSISTED_KEYS if k not in HEAVY_KEYS]

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


def _light_store_file(workspace_id: str) -> str:
    return os.path.join(_safe_dir(workspace_id), "current_light.pkl")


def _atomic_pickle(payload: dict, store_file: str) -> None:
    tmp_path = store_file + ".tmp"
    with open(tmp_path, "wb") as f:
        pickle.dump(payload, f)
    os.replace(tmp_path, store_file)  # atomic on POSIX - no half-written files


def save(session_state, workspace_id: str) -> None:
    """FULL save - writes both the heavy (dataset) file and the light
    (dashboard config) file. Best-effort: never raises, a failed save just
    means the next successful save wins.

    Call this when the dataset itself may have changed (new file loaded,
    workspace switch, first save of a session). For frequent UI interactions
    that only touch dashboard config (pin/unpin, add/remove chart, slicer
    tweak...), use save_light() instead - it's much cheaper because it never
    touches the dataset."""
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        heavy_payload = {k: session_state.get(k) for k in HEAVY_KEYS}
        _atomic_pickle(heavy_payload, _store_file(workspace_id))
        light_payload = {k: session_state.get(k) for k in LIGHT_KEYS}
        _atomic_pickle(light_payload, _light_store_file(workspace_id))
    except Exception:
        pass


def save_light(session_state, workspace_id: str) -> None:
    """Cheap save - writes ONLY the small dashboard-config keys (pinned KPIs,
    charts, slicers, dashboard name, pivot reports, filters). Never touches
    the (possibly large) dataset file, so this is fast enough to call after
    every single click. Best-effort: never raises."""
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        light_payload = {k: session_state.get(k) for k in LIGHT_KEYS}
        _atomic_pickle(light_payload, _light_store_file(workspace_id))
    except Exception:
        pass


def load(workspace_id: str):
    """Returns the saved {key: value} dict for this workspace (heavy +
    light merged), or None if nothing has been saved yet at all (or the
    heavy file is unreadable/corrupted, in which case we treat it as empty
    rather than crashing the app). Missing/corrupted light data alone
    doesn't wipe out the dataset - it just falls back to defaults for the
    dashboard-config keys."""
    store_file = _store_file(workspace_id)
    if not os.path.exists(store_file):
        return None
    try:
        with open(store_file, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    payload.update(load_light(workspace_id))
    return payload


def load_light(workspace_id: str) -> dict:
    """Returns just the saved dashboard-config dict for this workspace
    (cheap - never reads the dataset file). Returns {} if nothing saved yet
    or the file is unreadable."""
    light_file = _light_store_file(workspace_id)
    if not os.path.exists(light_file):
        return {}
    try:
        with open(light_file, "rb") as f:
            return pickle.load(f) or {}
    except Exception:
        return {}


def clear(workspace_id: str) -> None:
    """Deletes the saved workspace (heavy + light) from disk. Only call this
    from an explicit, admin-confirmed reset action - never automatically."""
    try:
        for store_file in (_store_file(workspace_id), _light_store_file(workspace_id)):
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


# ----------------------------------------------------------------------------
# AI Assistant chat history - per-workspace, self-expiring after 5 days.
# Kept OUT of PERSISTED_KEYS/LIGHT_KEYS on purpose: it has its own TTL-pruning
# read path below instead of being restored verbatim like the rest of the
# workspace, so old chats actually disappear instead of accumulating forever.
# ----------------------------------------------------------------------------
CHAT_HISTORY_TTL_SECONDS = 5 * 24 * 60 * 60  # 5 days


def _chat_history_file(workspace_id: str) -> str:
    return os.path.join(_safe_dir(workspace_id), "chat_history.pkl")


def save_chat_history(history: list, workspace_id: str) -> None:
    """Best-effort save of the AI Assistant chat log for this workspace.
    Each turn should carry a 'ts' (time.time()) so load_chat_history() can
    prune it once it's more than 5 days old."""
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        _atomic_pickle(history, _chat_history_file(workspace_id))
    except Exception:
        pass


def load_chat_history(workspace_id: str) -> list:
    """Returns this workspace's saved chat history, automatically dropping
    (and re-saving without) any turn older than 5 days - old chats are never
    shown again and get pruned off disk the next time this runs."""
    path = _chat_history_file(workspace_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            history = pickle.load(f) or []
    except Exception:
        return []
    cutoff = time.time() - CHAT_HISTORY_TTL_SECONDS
    fresh = [turn for turn in history if turn.get("ts", 0) >= cutoff]
    if len(fresh) != len(history):
        save_chat_history(fresh, workspace_id)
    return fresh
