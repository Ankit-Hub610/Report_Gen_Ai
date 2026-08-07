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
SESSIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.json")
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60   # 7 days of inactivity before a "stay logged in" link expires

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
        {"username": u, "role": info["role"], "workspace_id": info.get("workspace_id", u), "email": info.get("email", "")}
        for u, info in store["users"].items()
    ]


def get_role(username: str):
    store = _load_store()
    info = store["users"].get((username or "").strip())
    return info["role"] if info else None


def list_client_usernames():
    """Every account with role == client - used to populate the 'link this
    viewer to a client's data' dropdown in the Admin Panel."""
    return [u["username"] for u in list_users() if u["role"] == ROLE_CLIENT]


def create_or_update_user(username: str, password_plain: str, role: str, workspace_id: str = None, email: str = None):
    """Creates a new account or updates an existing one's password/role/link.
    workspace_id: which data workspace this account should read/write.
      - If omitted/blank, defaults to the account's own username (a fresh,
        fully independent workspace - the normal case for admin & client
        accounts, and for a standalone viewer).
      - For a viewer account, pass an existing CLIENT's username here to
        link the viewer to that client's live data instead.
    email: optional, but required if the account should be able to use
      "Forgot password" (the reset link goes to this address)."""
    username = (username or "").strip()
    if not username or not password_plain or role not in ALL_ROLES:
        raise ValueError("Username, password and a valid role are required.")
    store = _load_store()
    ws_id = (workspace_id or "").strip() or username
    entry = {"password_hash": _hash(password_plain), "role": role, "workspace_id": ws_id}
    if email:
        entry["email"] = email.strip().lower()
    elif username in store["users"] and store["users"][username].get("email"):
        entry["email"] = store["users"][username]["email"]   # keep existing email on an update that didn't touch it
    store["users"][username] = entry
    _save_store(store)


def set_email(username: str, email: str):
    store = _load_store()
    if username not in store["users"]:
        raise ValueError(f"User '{username}' does not exist.")
    store["users"][username]["email"] = (email or "").strip().lower()
    _save_store(store)


def get_email(username: str):
    store = _load_store()
    return store["users"].get((username or "").strip(), {}).get("email")


def find_username_by_email(email: str):
    """Case-insensitive lookup. Returns the username, or None if no account
    uses that email — callers should NOT reveal which case happened (always
    show the same generic message either way), to avoid leaking who has an
    account here."""
    email = (email or "").strip().lower()
    if not email:
        return None
    store = _load_store()
    for uname, info in store["users"].items():
        if (info.get("email") or "").lower() == email:
            return uname
    return None


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


# ==================================================================================
# "STAY LOGGED IN" SESSION TOKENS
# ----------------------------------------------------------------------------------
# Streamlit wipes st.session_state on a genuine browser refresh/reload (a new
# in-memory session starts from scratch), which is why a plain "if not
# session_state.authenticated: show login" was kicking people back to the
# login screen just from hitting refresh. To fix that WITHOUT weakening
# security (i.e. without keeping people logged in forever), we hand out a
# random, unguessable token on successful login, put it in the page's URL
# (?s=<token>) so the browser keeps re-sending it on every reload, and look
# it up here — a token only proves who you are for SESSION_LIFETIME_SECONDS
# and gets deleted immediately on logout or password change.
# ==================================================================================

def _load_sessions() -> dict:
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    now = time.time()
    return {tok: v for tok, v in data.items() if v.get("expires", 0) > now}   # drop expired on every read


def _save_sessions(sessions: dict):
    with open(SESSIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sessions, f, indent=2)


def create_session(username: str) -> str:
    """Called right after a successful login. Returns a token to put in the URL."""
    sessions = _load_sessions()
    token = secrets.token_urlsafe(24)
    sessions[token] = {"username": username, "expires": time.time() + SESSION_LIFETIME_SECONDS}
    _save_sessions(sessions)
    return token


def resolve_session(token: str):
    """Given a token from the URL, returns the username it belongs to (and
    silently refreshes its expiry, so an active user's link keeps working),
    or None if the token is missing/expired/was logged out."""
    if not token:
        return None
    sessions = _load_sessions()
    entry = sessions.get(token)
    if not entry:
        return None
    entry["expires"] = time.time() + SESSION_LIFETIME_SECONDS   # sliding expiry: still-active users don't get logged out
    _save_sessions(sessions)
    return entry["username"]


def destroy_session(token: str):
    """Called on explicit Logout — invalidates the link immediately."""
    if not token:
        return
    sessions = _load_sessions()
    if token in sessions:
        del sessions[token]
        _save_sessions(sessions)


# ==================================================================================
# "FORGOT PASSWORD" RESET TOKENS
# ----------------------------------------------------------------------------------
# Same idea as session tokens above, but much shorter-lived (30 minutes) and
# single-use: a client who forgot their password gets emailed a link
# containing one of these; visiting it lets them set a new password ONE
# time, then the token is deleted immediately so the same email link can't
# be reused later.
# ==================================================================================
RESET_LIFETIME_SECONDS = 30 * 60
RESETS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "password_resets.json")


def _load_resets() -> dict:
    if not os.path.exists(RESETS_FILE):
        return {}
    try:
        with open(RESETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    now = time.time()
    return {tok: v for tok, v in data.items() if v.get("expires", 0) > now}


def _save_resets(resets: dict):
    with open(RESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(resets, f, indent=2)


def create_password_reset_token(username: str) -> str:
    resets = _load_resets()
    token = secrets.token_urlsafe(24)
    resets[token] = {"username": username, "expires": time.time() + RESET_LIFETIME_SECONDS}
    _save_resets(resets)
    return token


def resolve_password_reset_token(token: str):
    """Returns the username the token belongs to, or None if it's
    missing/expired/already used. Does NOT delete it — call
    consume_password_reset_token() only after the new password is
    successfully set."""
    if not token:
        return None
    return _load_resets().get(token, {}).get("username")


def consume_password_reset_token(token: str):
    resets = _load_resets()
    if token in resets:
        del resets[token]
        _save_resets(resets)
