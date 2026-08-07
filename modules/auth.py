"""
auth.py
-------
Role-based login system with THREE roles (multi-tenant):

  - "admin"  : full app control. Can open the hidden Admin Panel (behind a
               separate admin-only login form) to create/delete accounts of
               any role, reset ANYONE's password, and change their own
               password. Admin does not own a data workspace of their own -
               instead, on every page, admin picks (via a "View as" control)
               which client/viewer's workspace to look at and manage.
  - "client" : a business/customer account. Has their OWN independent data
               workspace - can upload/manage data, build dashboards, etc.
               Cannot see the Admin Panel or any other client's data.
  - "viewer" : a read-only report-viewer account, typically an employee of a
               client. Can only VIEW dashboards/reports - cannot upload data,
               cannot add/remove/customize charts or KPIs. Every viewer is
               linked to exactly one workspace:
                 * by default, a brand-new independent (empty) workspace, OR
                 * an existing CLIENT's workspace, if the admin explicitly
                   links them when creating the account (so that viewer sees
                   that client's live data).

Credentials are stored (hashed, never in plain text) in credentials.json
next to app.py, as:

    {
      "users": {
        "admin":    {"password_hash": "...", "role": "admin",  "workspace_id": "admin"},
        "acme_co":  {"password_hash": "...", "role": "client", "workspace_id": "acme_co"},
        "acme_emp": {"password_hash": "...", "role": "viewer", "workspace_id": "acme_co"}
      }
    }

"workspace_id" is the key used by workspace_store.py to decide which on-disk
folder of data/dashboard state an account reads and writes. Every account's
workspace_id defaults to its own username (fully independent data) unless a
viewer is explicitly linked to another (client) account's workspace_id at
creation/edit time.

Default admin login on first run:  username: admin   password: admin123
Change it immediately from the Admin Panel (🔐 icon on the login screen).

Old single-user and old two-role (admin/viewer, no workspace_id) credentials
files are auto-migrated the first time this module loads, so nobody gets
locked out by this upgrade.
"""

import hashlib
import json
import os
import secrets
import time

CRED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json")
SESSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.json")

# How long a "remember me" login survives a browser refresh / tab reopen with
# no explicit logout. Long on purpose (Streamlit reruns the whole script on
# every browser tab refresh, which used to wipe st.session_state and bounce
# people back to the login screen even though they never clicked Logout).
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days

ROLE_ADMIN = "admin"
ROLE_CLIENT = "client"
ROLE_VIEWER = "viewer"
ALL_ROLES = (ROLE_ADMIN, ROLE_CLIENT, ROLE_VIEWER)


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode("utf-8")).hexdigest()


def _default_store():
    return {"users": {"admin": {"password_hash": _hash("admin123"), "role": ROLE_ADMIN, "workspace_id": "admin"}}}


def _migrate_if_old_format(raw: dict) -> dict:
    """Handles two older formats so existing installs never get locked out:
      1. Single-user format: {"username": "...", "password_hash": "..."}
      2. Two-role multi-user format (pre-Phase-4, no "workspace_id" field):
         {"users": {"name": {"password_hash": "...", "role": "admin"|"viewer"}}}
    In case 2, every existing account becomes its own independent workspace
    (workspace_id = its own username) - nobody's data merges with anyone
    else's just because of this upgrade. Existing "viewer" accounts keep
    their own separate workspace too (they simply weren't linked to a
    client before Phase 4 existed, so there is nothing to link them to).
    """
    if "username" in raw and "password_hash" in raw:
        raw = {"users": {raw["username"]: {"password_hash": raw["password_hash"], "role": ROLE_ADMIN}}}

    if "users" not in raw or not isinstance(raw["users"], dict):
        return _default_store()

    for uname, info in raw["users"].items():
        info.setdefault("workspace_id", uname)
        if info.get("role") not in ALL_ROLES:
            info["role"] = ROLE_VIEWER
    return raw


def _load_store() -> dict:
    if not os.path.exists(CRED_FILE):
        store = _default_store()
        _save_store(store)
        return store
    try:
        with open(CRED_FILE, "r") as f:
            raw = json.load(f)
        before = json.dumps(raw, sort_keys=True)
        store = _migrate_if_old_format(raw)
        after = json.dumps(store, sort_keys=True)
        if before != after:
            _save_store(store)  # persist the migration so it only happens once
        return store
    except Exception:
        return _default_store()


def _save_store(store: dict):
    with open(CRED_FILE, "w") as f:
        json.dump(store, f, indent=2)


# --------------------------------------------------------------------------------
# LOGIN
# --------------------------------------------------------------------------------
def verify_login(username: str, password_plain: str):
    """Returns the role string ("admin"/"client"/"viewer") on success, or None."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if user and _hash(password_plain or "") == user["password_hash"]:
        return user["role"]
    return None


def verify_admin_login(username: str, password_plain: str) -> bool:
    """Stricter check used ONLY by the separate Admin Panel login form -
    succeeds only for accounts with role == admin, even if the password is
    otherwise correct for a non-admin account of the same name (shouldn't
    happen, but this keeps the two login paths fully independent)."""
    return verify_login(username, password_plain) == ROLE_ADMIN


def get_workspace_id(username: str) -> str:
    """The data workspace this account reads/writes. Falls back to the
    username itself if the account is missing/unknown, so callers always
    get *something* usable rather than None."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if user:
        return user.get("workspace_id") or username
    return (username or "").strip()


# --------------------------------------------------------------------------------
# USER MANAGEMENT (admin-only - callers must gate this behind role == "admin")
# --------------------------------------------------------------------------------
def list_users():
    store = _load_store()
    return [
        {"username": u, "role": info["role"], "workspace_id": info.get("workspace_id", u)}
        for u, info in store["users"].items()
    ]


def list_client_usernames():
    """Every account with role == client - used to populate the 'link this
    viewer to a client's data' dropdown in the Admin Panel."""
    return [u["username"] for u in list_users() if u["role"] == ROLE_CLIENT]


def create_or_update_user(username: str, password_plain: str, role: str, workspace_id: str = None):
    """Creates a new account or updates an existing one's password/role/link.
    workspace_id: which data workspace this account should read/write.
      - If omitted/blank, defaults to the account's own username (a fresh,
        fully independent workspace - the normal case for admin & client
        accounts, and for a standalone viewer).
      - For a viewer account, pass an existing CLIENT's username here to
        link the viewer to that client's live data instead."""
    username = (username or "").strip()
    if not username or not password_plain or role not in ALL_ROLES:
        raise ValueError("Username, password and a valid role are required.")
    store = _load_store()
    ws_id = (workspace_id or "").strip() or username
    store["users"][username] = {"password_hash": _hash(password_plain), "role": role, "workspace_id": ws_id}
    _save_store(store)


def change_password(username: str, new_password_plain: str):
    store = _load_store()
    if username not in store["users"]:
        raise ValueError(f"User '{username}' does not exist.")
    if not new_password_plain:
        raise ValueError("New password cannot be empty.")
    store["users"][username]["password_hash"] = _hash(new_password_plain)
    _save_store(store)


def delete_user(username: str, acting_username: str):
    store = _load_store()
    if username not in store["users"]:
        raise ValueError(f"User '{username}' does not exist.")
    if username == acting_username:
        raise ValueError("You can't delete the account you're currently logged in with.")
    admins_left = sum(1 for u in store["users"].values() if u["role"] == ROLE_ADMIN)
    if store["users"][username]["role"] == ROLE_ADMIN and admins_left <= 1:
        raise ValueError("Can't delete the last remaining admin account.")
    del store["users"][username]
    _save_store(store)


def user_exists(username: str) -> bool:
    store = _load_store()
    return (username or "").strip() in store["users"]


# --------------------------------------------------------------------------------
# PERSISTENT LOGIN SESSIONS (survive a browser tab refresh, without logging
# anyone in forever). A random token is put in the page's URL query string
# on login. On every script run (including the rerun caused by hitting the
# browser's Refresh button) app.py checks that token against this file and,
# if it's still valid, silently restores the session instead of showing the
# login screen. The token is only removed - and the session only ends -
# when the user clicks Logout, or after SESSION_TTL_SECONDS of not being
# used at all.
# --------------------------------------------------------------------------------
def _load_sessions() -> dict:
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sessions(sessions: dict):
    try:
        with open(SESSION_FILE, "w") as f:
            json.dump(sessions, f, indent=2)
    except Exception:
        pass


def _prune_expired(sessions: dict) -> dict:
    now = time.time()
    return {tok: s for tok, s in sessions.items() if s.get("expires_at", 0) > now}


def create_session(username: str, role: str, workspace_id: str) -> str:
    """Called right after a successful login. Returns a random token that the
    caller should stash in st.query_params so it survives a page refresh."""
    sessions = _prune_expired(_load_sessions())
    token = secrets.token_urlsafe(32)
    sessions[token] = {
        "username": username,
        "role": role,
        "workspace_id": workspace_id,
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }
    _save_sessions(sessions)
    return token


def validate_session(token: str):
    """Returns {"username","role","workspace_id"} if the token is a live
    session, else None. Also re-checks the account still exists (in case it
    was deleted by an admin since the token was issued) and refreshes the
    token's expiry so an actively-used tab never gets logged out mid-session."""
    if not token:
        return None
    sessions = _prune_expired(_load_sessions())
    sess = sessions.get(token)
    if not sess:
        return None
    if not user_exists(sess["username"]):
        del sessions[token]
        _save_sessions(sessions)
        return None
    sess["expires_at"] = time.time() + SESSION_TTL_SECONDS
    sessions[token] = sess
    _save_sessions(sessions)
    return {"username": sess["username"], "role": sess["role"], "workspace_id": sess["workspace_id"]}


def destroy_session(token: str):
    """Called on explicit Logout - this is the only normal way a session ends
    before it naturally expires."""
    if not token:
        return
    sessions = _load_sessions()
    if token in sessions:
        del sessions[token]
        _save_sessions(sessions)
