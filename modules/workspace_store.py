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

import base64
import json
import os
import pickle
import re
import time
import uuid

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
    "dashboard_zoom",     # {chart_key: [start_pct, end_pct]} — per-chart zoom window, so it survives restarts
                          # and the PDF export (rendered from the same figure) matches what's on screen
    "dashboard_name",
    "intel_action_checks",   # 🧠 Intelligence Report — ticked/unticked state of the Top Actions checklist
    "intel_language",        # 🧠 Intelligence Report — last-picked narrative language (English/Hindi)
    "intel_role_overrides",  # 🧠 Intelligence Report — user-confirmed column-role mapping
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
# Slides — multiple independent datasets/dashboards per client workspace.
# ----------------------------------------------------------------------------
# One client (workspace_id) can hold several named "slides" (like browser
# tabs) - each with its OWN dataset, filters, Boss Dashboard, Custom Builder
# content, AI chat log and Intelligence snapshots, completely independent of
# every other slide. Uploading new data into one slide never touches another.
#
# The registry (which slides exist + their display names) lives in ONE small
# JSON file in the workspace's own folder: workspace_state/<workspace_id>/
# slides.json. The actual per-slide data reuses every function above
# (save/load/save_light/load_light/chat history/intel snapshots) unchanged -
# callers just pass slide_storage_id(workspace_id, slide_id) instead of the
# plain workspace_id, and each (workspace, slide) pair naturally lands in its
# own folder on disk.
#
# Backward compatibility: the first slide is always id "default", and
# slide_storage_id() maps it straight to the PLAIN workspace_id (no suffix) -
# so every account that existed before this feature keeps working with zero
# migration, its existing data just becomes "Slide 1" automatically the
# first time list_slides()/get_active_slide() is called for it.
def _slides_file(workspace_id: str) -> str:
    return os.path.join(_safe_dir(workspace_id), "slides.json")


def _default_slides_registry() -> dict:
    return {"slides": [{"id": "default", "name": "Slide 1"}], "active": "default"}


def _load_slides_registry(workspace_id: str) -> dict:
    path = _slides_file(workspace_id)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
            if data.get("slides"):
                return data
        except Exception:
            pass
    # Nothing saved yet (or the file is corrupted/empty) - (re)create the
    # default registry so every pre-existing workspace's data is preserved
    # as "Slide 1" rather than appearing to have vanished.
    data = _default_slides_registry()
    _save_slides_registry(workspace_id, data)
    return data


def _save_slides_registry(workspace_id: str, data: dict) -> None:
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        tmp = _slides_file(workspace_id) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _slides_file(workspace_id))
    except Exception:
        pass


def slide_storage_id(workspace_id: str, slide_id: str) -> str:
    """Combines a real workspace_id + slide_id into the single string every
    other function in this module treats as 'workspace_id' - each
    (workspace, slide) pair gets its own folder on disk. The 'default' slide
    reuses the PLAIN workspace_id (no suffix) so existing pre-multi-slide
    data keeps working without any migration."""
    if not slide_id or slide_id == "default":
        return workspace_id
    safe_slide = _SAFE_ID_RE.sub("_", str(slide_id))
    return f"{workspace_id}__slide__{safe_slide}"


def list_slides(workspace_id: str) -> list:
    """[{'id':..., 'name':...}, ...] for this workspace, in creation order.
    Always has at least the auto-created 'default' slide."""
    return _load_slides_registry(workspace_id)["slides"]


def get_active_slide(workspace_id: str) -> str:
    """Which slide this workspace was last left on - so a fresh login (or a
    different browser session sharing the account) opens on the same slide
    instead of always resetting to the first one."""
    return _load_slides_registry(workspace_id).get("active", "default")


def set_active_slide(workspace_id: str, slide_id: str) -> None:
    data = _load_slides_registry(workspace_id)
    data["active"] = slide_id
    _save_slides_registry(workspace_id, data)


def create_slide(workspace_id: str, name: str = "") -> str:
    """Registers a new, empty slide and returns its id. Does not create any
    data itself - the caller switches the session onto this id and then
    uploads a fresh dataset, which is what actually gives the slide its own
    saved data on disk."""
    data = _load_slides_registry(workspace_id)
    new_id = uuid.uuid4().hex[:8]
    display_name = (name or "").strip() or f"Slide {len(data['slides']) + 1}"
    data["slides"].append({"id": new_id, "name": display_name})
    data["active"] = new_id
    _save_slides_registry(workspace_id, data)
    return new_id


def rename_slide(workspace_id: str, slide_id: str, new_name: str) -> None:
    new_name = (new_name or "").strip()
    if not new_name:
        return
    data = _load_slides_registry(workspace_id)
    for s in data["slides"]:
        if s["id"] == slide_id:
            s["name"] = new_name
            break
    _save_slides_registry(workspace_id, data)


def reset_slides_registry(workspace_id: str) -> None:
    """Wipes the slide registry back to a single empty 'default' slide.
    Does NOT delete any per-slide data files itself — callers that want a
    full wipe should clear() each slide's storage id first (see the Admin
    Panel 'Reset Workspace Data' flow in app.py)."""
    _save_slides_registry(workspace_id, _default_slides_registry())


def delete_slide(workspace_id: str, slide_id: str) -> str:
    """Removes a slide's registry entry AND every file it ever saved
    (dataset, dashboard config, chat history, intel snapshots). Refuses to
    delete the last remaining slide (a workspace always keeps at least one).
    Returns the slide_id that should now be made active."""
    data = _load_slides_registry(workspace_id)
    if len(data["slides"]) <= 1:
        return data.get("active", "default")
    remaining = [s for s in data["slides"] if s["id"] != slide_id]
    storage_id = slide_storage_id(workspace_id, slide_id)
    clear(storage_id)
    for path in (_intel_snapshots_file(storage_id), _chat_history_file(storage_id)):
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
    data["slides"] = remaining
    if data.get("active") == slide_id:
        data["active"] = remaining[0]["id"]
    _save_slides_registry(workspace_id, data)
    return data["active"]


# ----------------------------------------------------------------------------
# Intelligence Report snapshots - per-workspace, so the report can show
# "vs your last report" deltas. Kept small on purpose: only the headline
# numbers (not the raw dataset, not the AI narrative text) are stored, so
# this file stays tiny even after many snapshots. Capped to the most recent
# MAX_SNAPSHOTS so it never grows unbounded.
# ----------------------------------------------------------------------------
MAX_INTEL_SNAPSHOTS = 20


def _intel_snapshots_file(workspace_id: str) -> str:
    return os.path.join(_safe_dir(workspace_id), "intel_snapshots.pkl")


def save_intel_snapshot(headline: dict, workspace_id: str) -> None:
    """headline should be a small, pickle-friendly dict of the report's key
    numbers (e.g. total_revenue, total_profit, margin, row_count, ts). Never
    stores the DataFrame or the full narrative - just enough to compute a
    'vs last time' delta later. Best-effort, never raises."""
    try:
        d = _safe_dir(workspace_id)
        os.makedirs(d, exist_ok=True)
        path = _intel_snapshots_file(workspace_id)
        existing = []
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    existing = pickle.load(f) or []
            except Exception:
                existing = []
        existing.append(headline)
        existing = existing[-MAX_INTEL_SNAPSHOTS:]
        _atomic_pickle(existing, path)
    except Exception:
        pass


def load_intel_snapshots(workspace_id: str) -> list:
    path = _intel_snapshots_file(workspace_id)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            return pickle.load(f) or []
    except Exception:
        return []


# ----------------------------------------------------------------------------
# App branding (sidebar title) - GLOBAL, not per-workspace. Every account sees
# the same brand text/style, so it's admin-only and lives in its own file
# instead of PERSISTED_KEYS (which is per-workspace_id).
# ----------------------------------------------------------------------------
_BRAND_FILE = os.path.join(STORE_ROOT, "_branding.pkl")

# ----------------------------------------------------------------------------
# OPTIONAL GitHub-backed persistence (branding only, for now)
# ----------------------------------------------------------------------------
# Local disk (workspace_state/) is NOT persistent on platforms with an
# ephemeral filesystem (e.g. Streamlit Community Cloud) — the container gets
# rebuilt from the git repo whenever the app sleeps from inactivity and is
# reopened, or on every redeploy, wiping anything that was only ever written
# to local disk at runtime. That's exactly why branding (logo, colors, glow)
# was disappearing and needing to be re-set up on every fresh open.
#
# Fix: if these two secrets are configured (Settings -> Secrets on Streamlit
# Cloud, or a local .streamlit/secrets.toml, or plain env vars), branding is
# ALSO committed straight to the GitHub repo via the Contents API, so it's
# baked into the exact same source the container rebuilds from and survives
# every restart/redeploy:
#
#     GITHUB_TOKEN  = "ghp_..."          # fine-grained PAT, 'Contents: Read and write' on this repo only
#     GITHUB_REPO   = "yourname/yourrepo"
#     GITHUB_BRANCH = "main"             # optional, defaults to "main"
#
# Without these set, behavior is UNCHANGED from before (local-disk pickle
# only) — fine for local/dev runs or any host with a real persistent disk.
# Best-effort throughout: a failed GitHub call never crashes the app, it just
# silently falls back to whatever's on local disk for the rest of that run.
GITHUB_BRAND_PATH = "workspace_state/_branding.pkl"


def _github_config():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    branch = os.environ.get("GITHUB_BRANCH")
    try:
        import streamlit as st
        token = st.secrets.get("GITHUB_TOKEN", token)
        repo = st.secrets.get("GITHUB_REPO", repo)
        branch = st.secrets.get("GITHUB_BRANCH", branch)
    except Exception:
        pass
    if token and repo:
        return {"token": token, "repo": repo.strip("/"), "branch": (branch or "main")}
    return None


def _github_headers(cfg):
    return {"Authorization": f"token {cfg['token']}", "Accept": "application/vnd.github+json"}


def _github_get_file(cfg, path):
    """Returns (raw_bytes, sha) or (None, None) if missing/unreachable."""
    try:
        import requests
        url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
        r = requests.get(url, headers=_github_headers(cfg), params={"ref": cfg["branch"]}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return base64.b64decode(data["content"]), data.get("sha")
    except Exception:
        pass
    return None, None


def _github_put_file(cfg, path, content_bytes, message):
    try:
        import requests
        url = f"https://api.github.com/repos/{cfg['repo']}/contents/{path}"
        _, sha = _github_get_file(cfg, path)
        payload = {"message": message, "content": base64.b64encode(content_bytes).decode(), "branch": cfg["branch"]}
        if sha:
            payload["sha"] = sha
        r = requests.put(url, headers=_github_headers(cfg), json=payload, timeout=15)
        return r.status_code in (200, 201)
    except Exception:
        return False


def save_branding(brand: dict) -> None:
    try:
        os.makedirs(STORE_ROOT, exist_ok=True)
        tmp_path = _BRAND_FILE + ".tmp"
        with open(tmp_path, "wb") as f:
            pickle.dump(brand, f)
        os.replace(tmp_path, _BRAND_FILE)
    except Exception:
        pass
    cfg = _github_config()
    if cfg:
        try:
            _github_put_file(cfg, GITHUB_BRAND_PATH, pickle.dumps(brand), "Update app branding (auto-saved)")
        except Exception:
            pass


def load_branding():
    """Returns the saved brand dict, or None if nothing's been saved yet /
    unreadable (treated as 'use defaults', never crashes). Tries GitHub first
    (if configured) since that's the copy that actually survives a restart;
    falls back to local disk otherwise, which is always tried and kept as a
    fast local mirror for the rest of this run."""
    cfg = _github_config()
    if cfg:
        content, _ = _github_get_file(cfg, GITHUB_BRAND_PATH)
        if content:
            try:
                brand = pickle.loads(content)
                try:
                    os.makedirs(STORE_ROOT, exist_ok=True)
                    with open(_BRAND_FILE, "wb") as f:
                        pickle.dump(brand, f)
                except Exception:
                    pass
                return brand
            except Exception:
                pass
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
