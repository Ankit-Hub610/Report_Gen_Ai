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
import math
import os
import secrets
import time

CRED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json")
SESSIONS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sessions.json")
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60   # 7 days of inactivity before a "stay logged in" link expires
TRIAL_DAYS = 14   # how long a brand-new "free" account gets full-but-capped access before it locks

ROLE_ADMIN = "admin"
ROLE_CLIENT = "client"
ROLE_VIEWER = "viewer"
ROLE_REPORT_VIEWER = "report_viewer"   # restricted viewer a CLIENT can self-serve create for
                                        # their own boss/manager — Boss Dashboard page ONLY
                                        # (full access there: view, PDF export, manage slicers),
                                        # nothing else in the app. Different from ROLE_VIEWER,
                                        # which an admin creates and which can look at every
                                        # page (read-only) — kept as-is for backward compatibility.
ALL_ROLES = (ROLE_ADMIN, ROLE_CLIENT, ROLE_VIEWER, ROLE_REPORT_VIEWER)


def _hash(pw: str, salt: bytes = None) -> str:
    """Salted PBKDF2-SHA256 (200k iterations) — stored as 'saltHex:hashHex'.
    Old accounts (created before this change) had a bare unsalted-SHA256 hex
    string as their password_hash; _verify_password below still recognizes
    that legacy format so nobody gets locked out, and silently upgrades that
    account to the new salted format the next time they successfully log in."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(pw: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    if ":" in stored_hash:
        salt_hex, _, dk_hex = stored_hash.partition(":")
        try:
            salt = bytes.fromhex(salt_hex)
        except ValueError:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 200_000)
        return dk.hex() == dk_hex
    # legacy unsalted sha256 (pre-upgrade accounts)
    return hashlib.sha256(pw.encode("utf-8")).hexdigest() == stored_hash


def _is_legacy_hash(stored_hash: str) -> bool:
    return bool(stored_hash) and ":" not in stored_hash


def _default_store():
    return {"users": {"admin": {"password_hash": _hash("admin123"), "role": ROLE_ADMIN,
                                "workspace_id": "admin", "plan": "standard"}}}


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
        info.setdefault("plan", "standard")   # existing accounts stay unlimited — nobody gets newly capped by surprise
        # Standard = "standard" with no expiry (permanent) unless a duration was
        # set (e.g. via a UPI approval). Existing pre-this-feature accounts get
        # no expiry at all, same "unlimited" behaviour they already had.
        info.setdefault("standard_expires_at", None)
        info.setdefault("billing_cycle", None)
        info.setdefault("trial_started_at", None)
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
    """Returns the role string ("admin"/"client"/"viewer") on success, or None.
    On a successful login through a legacy (pre-salt) password hash, silently
    upgrades that account's stored hash to the new salted format - no user
    action needed, no disruption, just quietly more secure from here on."""
    store = _load_store()
    uname = (username or "").strip()
    user = store["users"].get(uname)
    if user and _verify_password(password_plain or "", user["password_hash"]):
        if _is_legacy_hash(user["password_hash"]):
            user["password_hash"] = _hash(password_plain or "")
            _save_store(store)
        return user["role"]
    return None


def get_plan(username: str) -> str:
    """Returns the RAW stored base plan: 'standard' or 'free'. This does NOT
    account for expiry (a 'standard' account whose subscription lapsed still
    shows 'standard' here) - use get_effective_plan() for the real, current
    access level. This one stays around for places that just need the base
    plan (e.g. the admin panel's own free/standard branch)."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    return (user or {}).get("plan", "standard")


def get_trial_status(username: str) -> dict:
    """For a 'free' account: how much of its TRIAL_DAYS trial is left.
    Recomputed fresh from trial_started_at every call - never cached - so it
    is always correct the moment it's asked, not just at login."""
    store = _load_store()
    info = store["users"].get((username or "").strip()) or {}
    started = info.get("trial_started_at")
    if not started:
        return {"expired": False, "days_left": TRIAL_DAYS}
    elapsed_days = (time.time() - started) / 86400.0
    days_left = max(0, math.ceil(TRIAL_DAYS - elapsed_days))
    return {"expired": elapsed_days >= TRIAL_DAYS, "days_left": days_left}


def get_subscription_status(username: str) -> dict:
    """For a 'standard' account: when (if ever) it expires. expires_at is
    None for a permanent/manual grant (no auto-expiry). Recomputed fresh
    every call, same reasoning as get_trial_status()."""
    store = _load_store()
    info = store["users"].get((username or "").strip()) or {}
    expires_at = info.get("standard_expires_at")
    billing_cycle = info.get("billing_cycle")
    if not expires_at:
        return {"expires_at": None, "billing_cycle": billing_cycle, "days_left": None, "expired": False}
    days_left = max(0, math.ceil((expires_at - time.time()) / 86400.0))
    return {"expires_at": expires_at, "billing_cycle": billing_cycle,
            "days_left": days_left, "expired": time.time() >= expires_at}


def get_effective_plan(username: str) -> str:
    """The REAL, current access level: 'free', 'standard', or
    'expired_standard' (was Standard, subscription lapsed, needs to renew).
    Always recomputed fresh from disk - this is deliberately NOT cached
    anywhere beyond a single call, specifically so that an admin's approval
    (or a subscription's natural expiry) shows up the moment app.py next
    calls this - on literally the next rerun (any click/page-change) for
    everyone, plus near-instantly on the Plans page itself via its own
    short auto-refresh timer - instead of only after logging out and back
    in, which was the actual bug being fixed here."""
    store = _load_store()
    info = store["users"].get((username or "").strip()) or {}
    base = info.get("plan", "standard")
    if base == "standard":
        sub = get_subscription_status(username)
        if sub["expires_at"] and sub["expired"]:
            return "expired_standard"
        return "standard"
    return "free"


def set_plan(username: str, plan: str, duration_days: int = None, billing_cycle: str = None):
    """Admin action - approving a UPI request, a manual grant, a downgrade,
    or a reversal all go through here.
      plan: "standard" or "free".
      duration_days: for "standard", how many days from NOW it's valid
        (e.g. 30 for a monthly approval, 365 for yearly). Omit/None for a
        permanent manual grant with no auto-expiry (e.g. admin comps someone
        via the 'Upgrade to Standard (unlimited)' button).
      billing_cycle: "monthly" / "yearly" / None - display-only, doesn't
        affect anything else."""
    store = _load_store()
    username = (username or "").strip()
    if username not in store["users"]:
        raise ValueError(f"Unknown user: {username}")
    info = store["users"][username]
    info["plan"] = "standard" if plan == "standard" else "free"
    if info["plan"] == "standard":
        info["standard_expires_at"] = (time.time() + duration_days * 86400) if duration_days else None
        info["billing_cycle"] = billing_cycle
    else:
        info["standard_expires_at"] = None
        info["billing_cycle"] = None
        if not info.get("trial_started_at"):
            info["trial_started_at"] = time.time()   # fresh trial clock, per the "moved to Free" message in the Admin Panel
    _save_store(store)


def reset_trial(username: str):
    """Gives a 'free' account a fresh TRIAL_DAYS-day clock, without touching
    its role, password, or workspace."""
    store = _load_store()
    username = (username or "").strip()
    if username not in store["users"]:
        raise ValueError(f"Unknown user: {username}")
    store["users"][username]["trial_started_at"] = time.time()
    _save_store(store)


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
        {"username": u, "role": info["role"], "workspace_id": info.get("workspace_id", u),
         "email": info.get("email", ""), "plan": info.get("plan", "standard")}
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


def create_or_update_user(username: str, password_plain: str, role: str, workspace_id: str = None,
                          email: str = None, plan: str = None):
    """Creates a new account or updates an existing one's password/role/link.
    workspace_id: which data workspace this account should read/write.
      - If omitted/blank, defaults to the account's own username (a fresh,
        fully independent workspace - the normal case for admin & client
        accounts, and for a standalone viewer).
      - For a viewer account, pass an existing CLIENT's username here to
        link the viewer to that client's live data instead.
    email: optional, but required if the account should be able to use
      "Forgot password" (the reset link goes to this address).
    plan: "standard" (default, unlimited) or "free" (capped — see
      usage_limits.py for the actual limits). Lets you give trial/beta
      accounts real usage caps without touching their role or workspace."""
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
    if plan in ("standard", "free"):
        entry["plan"] = plan
    elif username in store["users"] and store["users"][username].get("plan"):
        entry["plan"] = store["users"][username]["plan"]   # keep existing plan on an update that didn't touch it
    else:
        entry["plan"] = "standard"

    existing = store["users"].get(username, {})
    entry["standard_expires_at"] = existing.get("standard_expires_at")
    entry["billing_cycle"] = existing.get("billing_cycle")
    entry["trial_started_at"] = existing.get("trial_started_at")
    if entry["plan"] == "free" and not entry["trial_started_at"]:
        entry["trial_started_at"] = time.time()   # first time this account is Free — trial clock starts now
    if entry["plan"] == "standard":
        # switching TO standard clears any leftover trial clock so a later
        # downgrade back to free starts a fresh trial, not a stale one
        entry["trial_started_at"] = None

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
