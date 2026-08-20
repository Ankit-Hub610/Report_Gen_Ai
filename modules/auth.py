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

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time

from . import supabase_store

TRIAL_DAYS = 14   # how long a brand-new "free" account gets before it's blocked pending upgrade

CRED_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "credentials.json")
CRED_BLOB_KEY = "credentials.json"   # Supabase app_kv key - see supabase_store.py
SESSION_LIFETIME_SECONDS = 7 * 24 * 60 * 60   # 7 days of inactivity before a "stay logged in" link expires

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
        if info.get("role") not in ALL_ROLES:
            info["role"] = ROLE_VIEWER
    return raw


def _load_store() -> dict:
    """Reads accounts. Tries Supabase FIRST (if configured) since that's the
    copy that actually survives a container rebuild on an ephemeral-disk
    host (Streamlit Community Cloud) - local disk is kept as a fast mirror
    for the rest of this run either way. Falls back to local disk (and then
    to a brand-new default store) if Supabase isn't configured/reachable, so
    an install with no Supabase secrets set behaves exactly as before."""
    blob = supabase_store.get_blob(CRED_BLOB_KEY)
    if blob:
        try:
            raw = json.loads(blob.decode("utf-8"))
            store = _migrate_if_old_format(raw)
            try:
                with open(CRED_FILE, "w") as f:
                    json.dump(store, f, indent=2)
            except Exception:
                pass
            return store
        except Exception:
            pass  # fall through to local disk below

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
    try:
        supabase_store.put_blob(CRED_BLOB_KEY, json.dumps(store, indent=2).encode("utf-8"))
    except Exception:
        pass


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
    """Returns the RAW stored plan — 'standard' (unlimited) or 'free' (capped —
    see usage_limits.py). Does NOT account for an expired subscription; for
    that, use get_effective_plan()."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    return (user or {}).get("plan", "standard")


def compute_trial_status(trial_start) -> dict:
    """Pure function: given a trial_start timestamp, returns
    {'days_left': int, 'expired': bool}. Shared by get_trial_status()
    (account-level trial) and get_demo_identity_trial_status() (per-person
    trial for a shared demo login - see get_or_create_demo_identity)."""
    if trial_start is None:
        return {"days_left": TRIAL_DAYS, "expired": False}
    elapsed_days = (time.time() - trial_start) / 86400.0
    days_left = max(0, math.ceil(TRIAL_DAYS - elapsed_days))
    return {"days_left": days_left, "expired": elapsed_days >= TRIAL_DAYS}


def get_trial_status(username: str) -> dict:
    """For a 'free' plan account: {'days_left': int, 'expired': bool}.
    trial_start is set the moment an account first becomes 'free' (at
    creation, via set_plan, or via reset_trial). If it's somehow missing
    (e.g. a very old record), it's initialized to right now so nobody is
    retroactively expired by this change.

    NOTE: for a SHARED demo account (is_shared_demo == True), this should
    NOT govern any individual visitor's trial countdown - every person
    sharing those credentials needs their OWN 15-day clock, tied to who
    THEY are (their email/phone), not to when the shared account itself was
    created. See get_or_create_demo_identity() / compute_trial_status() for
    that per-person version; app.py's login screen uses those instead of
    this function for a shared-demo login."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if not user:
        return {"days_left": None, "expired": False}
    start = user.get("trial_start")
    if start is None:
        start = time.time()
        user["trial_start"] = start
        _save_store(store)
    return compute_trial_status(start)


def get_subscription_status(username: str) -> dict:
    """For a 'standard' plan account: {'expires_at', 'days_left',
    'billing_cycle', 'expired'}. expires_at is None for a permanent
    (admin-granted, no-expiry) Standard account, in which case days_left
    and expired are meaningless (None / False)."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if not user:
        return {"expires_at": None, "days_left": None, "billing_cycle": None, "expired": False}
    expires_at = user.get("subscription_expires_at")
    billing_cycle = user.get("billing_cycle")
    if expires_at is None:
        return {"expires_at": None, "days_left": None, "billing_cycle": billing_cycle, "expired": False}
    days_left = max(0, math.ceil((expires_at - time.time()) / 86400.0))
    return {"expires_at": expires_at, "days_left": days_left,
            "billing_cycle": billing_cycle, "expired": time.time() >= expires_at}


def set_plan(username: str, plan: str, duration_days: int = None, billing_cycle: str = None):
    """Admin / payment-approval entry point for changing a user's plan
    (separate from create_or_update_user, which also touches password/role).
      plan == 'free': moves the account to Free and starts a brand new
        TRIAL_DAYS-day trial from right now — used both when an admin
        manually downgrades someone and when a mistaken payment approval
        is reversed.
      plan == 'standard': moves the account to Standard. duration_days
        (e.g. 30 or 365, from an approved UPI request) sets a real expiry;
        omitted/None means a permanent, admin-granted Standard account
        with no expiry."""
    if plan not in ("standard", "free"):
        raise ValueError("plan must be 'standard' or 'free'")
    store = _load_store()
    uname = (username or "").strip()
    user = store["users"].get(uname)
    if not user:
        raise ValueError(f"No such user: {username}")
    user["plan"] = plan
    if plan == "free":
        user["trial_start"] = time.time()
        user["subscription_expires_at"] = None
        user["billing_cycle"] = None
    else:
        user["billing_cycle"] = billing_cycle
        user["subscription_expires_at"] = (time.time() + duration_days * 86400) if duration_days else None
    _save_store(store)


def reset_trial(username: str):
    """Gives a free-plan account a fresh TRIAL_DAYS-day trial starting now,
    without touching its password/role/workspace."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if user:
        user["trial_start"] = time.time()
        _save_store(store)


def get_effective_plan(username: str) -> str:
    """The plan that should actually govern access RIGHT NOW — unlike
    get_plan(), this also accounts for a Standard subscription that has
    quietly lapsed. If a paid period has run out, the account is dropped
    back to 'free' (with a fresh trial) the moment anyone checks, so an
    expired subscription can never keep granting unlimited access just
    because nobody happened to notice. A 'free' account's trial *expiry*
    is intentionally NOT handled by downgrading here — this still returns
    'free' either way; app.py separately checks get_trial_status()
    ['expired'] to decide whether to show the trial-ended blocking screen,
    so the person still sees that screen instead of some other plan label."""
    store = _load_store()
    uname = (username or "").strip()
    user = store["users"].get(uname)
    if not user:
        return "standard"
    plan = user.get("plan", "standard")
    if plan == "standard":
        expires_at = user.get("subscription_expires_at")
        if expires_at is not None and time.time() >= expires_at:
            user["plan"] = "free"
            user["trial_start"] = time.time()
            user["subscription_expires_at"] = None
            user["billing_cycle"] = None
            _save_store(store)
            return "free"
    return plan


def verify_admin_login(username: str, password_plain: str) -> bool:
    """Stricter check used ONLY by the separate Admin Panel login form -
    succeeds only for accounts with role == admin, even if the password is
    otherwise correct for a non-admin account of the same name (shouldn't
    happen, but this keeps the two login paths fully independent)."""
    return verify_login(username, password_plain) == ROLE_ADMIN


def get_workspace_id(username: str) -> str:
    """The data workspace this account reads/writes. Falls back to the
    username itself if the account is missing/unknown, so callers always
    get *something* usable rather than None.

    NOTE: for a shared-demo account (is_shared_demo == True), this is NOT
    what's actually used at login — app.py generates a fresh temporary
    workspace_id per login instead, specifically so multiple people sharing
    the same demo username/password never see each other's data. This
    function still returns a stable fallback for any other caller that
    doesn't care about that (e.g. admin listings)."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    if user:
        return user.get("workspace_id") or username
    return (username or "").strip()


def is_shared_demo(username: str) -> bool:
    """True for an account marked as a shared demo login (see
    create_or_update_user's is_shared_demo param) — every separate login to
    such an account gets its own fresh, isolated temporary workspace
    instead of everyone sharing one workspace_id."""
    store = _load_store()
    user = store["users"].get((username or "").strip())
    return bool(user and user.get("is_shared_demo"))


# --------------------------------------------------------------------------------
# USER MANAGEMENT (admin-only - callers must gate this behind role == "admin")
# --------------------------------------------------------------------------------
def list_users():
    store = _load_store()
    return [
        {"username": u, "role": info["role"], "workspace_id": info.get("workspace_id", u),
         "email": info.get("email", ""), "plan": info.get("plan", "standard"),
         "is_shared_demo": bool(info.get("is_shared_demo"))}
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
                          email: str = None, plan: str = None, is_shared_demo: bool = False):
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
      accounts real usage caps without touching their role or workspace.
    is_shared_demo: mark this account as a SHARED DEMO login — meant to be
      handed out to multiple different people at once (same username +
      password for everyone). Every separate login to a shared-demo account
      gets its own fresh, isolated, temporary workspace (see app.py's login
      flow + auth.create_session/resolve_session), so no two people sharing
      these credentials ever see each other's uploaded data — unlike a
      normal account, where workspace_id is fixed and shared across every
      login. NOTE: this flag is applied exactly as passed on every call, so
      when just resetting a demo account's password, pass is_shared_demo=True
      again too, or it will silently turn back off (same as re-entering an
      existing account's other fields on this form)."""
    username = (username or "").strip()
    if not username or not password_plain or role not in ALL_ROLES:
        raise ValueError("Username, password and a valid role are required.")
    store = _load_store()
    ws_id = (workspace_id or "").strip() or username
    entry = {"password_hash": _hash(password_plain), "role": role, "workspace_id": ws_id,
             "is_shared_demo": bool(is_shared_demo)}
    if email:
        entry["email"] = email.strip().lower()
    elif username in store["users"] and store["users"][username].get("email"):
        entry["email"] = store["users"][username]["email"]   # keep existing email on an update that didn't touch it
    existing = store["users"].get(username, {})
    if plan in ("standard", "free"):
        entry["plan"] = plan
    elif existing.get("plan"):
        entry["plan"] = existing["plan"]   # keep existing plan on an update that didn't touch it
    else:
        entry["plan"] = "standard"

    # Trial / subscription bookkeeping (see get_trial_status / get_subscription_status
    # / get_effective_plan below). A brand-new "free" account, or one that just became
    # free right here, gets a fresh TRIAL_DAYS-day trial starting now; an existing free
    # account whose plan wasn't touched by this call keeps counting from whenever its
    # trial actually started. A "standard" account created/updated here (e.g. via the
    # Admin Panel's "Client / Standard" form) is treated as permanent/unlimited unless
    # something later calls set_plan() with a duration (e.g. an approved UPI payment).
    if entry["plan"] == "free":
        was_free_already = existing.get("plan") == "free"
        entry["trial_start"] = existing.get("trial_start") if was_free_already else time.time()
        entry["subscription_expires_at"] = None
        entry["billing_cycle"] = None
    else:
        entry["trial_start"] = existing.get("trial_start")
        entry["subscription_expires_at"] = existing.get("subscription_expires_at")
        entry["billing_cycle"] = existing.get("billing_cycle")

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
# login screen just from hitting refresh. The token itself lives in a real
# browser COOKIE (see app.py: _set_session_cookie), never in the URL — so
# copy-pasting the page's link into a different browser/device never carries
# a login with it, only the SAME browser that received the cookie stays in.
#
# STATELESS BY DESIGN (this was the actual bug — read on):
# An earlier version of this kept a server-side sessions.json mapping
# token -> username, similar to credentials.json. That looked fine locally,
# but on a host like Streamlit Community Cloud the app's local disk is NOT
# permanent (see DEPLOY_README.md) — every time the container sleeps from
# inactivity and wakes back up, or a new version is deployed, it's rebuilt
# fresh from GitHub. sessions.json isn't committed to GitHub (it's in
# .gitignore, same as credentials.json's old draft was), so it silently got
# wiped on every one of those restarts. The BROWSER still had a perfectly
# valid cookie, but the server had nothing left to look it up against — so
# a real refresh could still bounce someone back to the login screen,
# exactly like it did before this feature existed.
#
# Fix: the token is now SELF-VERIFYING (username + expiry + an HMAC
# signature), so resolving it needs no server-side lookup at all — it
# survives a container rebuild perfectly, the same way a password does
# (passwords work fine across restarts because credentials.json IS
# committed to GitHub). The one signing secret it needs is generated once
# and stored inside credentials.json itself, so it persists the same way.
#
# Trade-off, stated plainly: destroy_session() (explicit Logout) can no
# longer force-invalidate a specific token server-side, because there's no
# longer a server-side list to remove it from — clearing the browser
# cookie is what actually logs that browser out. A copied/stolen token
# would keep working until it naturally expires (SESSION_LIFETIME_SECONDS,
# 7 days), same as it always could between a login and the next password
# change. That's a reasonable trade for "refresh no longer randomly logs
# people out", but it's worth knowing about.
# ==================================================================================

def _get_session_secret() -> bytes:
    """The HMAC signing key for session tokens — generated once and stored
    inside credentials.json (which already persists across restarts/
    redeploys) so signatures keep validating even after the container is
    rebuilt from scratch."""
    store = _load_store()
    secret_hex = store.get("_session_secret")
    if not secret_hex:
        secret_hex = secrets.token_hex(32)
        store["_session_secret"] = secret_hex
        _save_store(store)
    return bytes.fromhex(secret_hex)


def create_session(username: str, workspace_id: str = None, trial_start: float = None) -> str:
    """Called right after a successful login (and again to silently extend
    an already-valid one — see app.py's cookie-restore block). Returns a
    signed, stateless token to store in the browser cookie.

    workspace_id: only passed for a SHARED DEMO account — embeds that
    login's own per-person workspace_id (see get_or_create_demo_identity)
    directly in the token, so it's self-verifying just like everything else
    here and survives a browser refresh without needing any server-side
    record. Omit for a normal account; the caller resolves workspace_id
    from auth.get_workspace_id(username) instead.

    trial_start: only passed alongside workspace_id for a shared demo
    login — that person's own trial_start (from the same identity record),
    so their remaining-days count also survives a refresh without a
    server-side lookup. Omit for a normal account (its trial_start already
    lives in credentials.json, keyed by username)."""
    secret = _get_session_secret()
    expires = int(time.time()) + SESSION_LIFETIME_SECONDS
    payload = f"{username}|{expires}|{workspace_id or ''}|{trial_start or ''}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")
    sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def resolve_session(token: str):
    """Given a token from the cookie, returns (username, workspace_id,
    trial_start) — workspace_id/trial_start are None unless this particular
    session embedded them (shared demo accounts only — see create_session).
    Returns (None, None, None) if the token is missing/malformed/tampered-
    with/expired. Verifies entirely from the token itself (signature +
    embedded expiry), so this keeps working right after a container
    rebuild."""
    if not token or "." not in token:
        return None, None, None
    payload_b64, _, sig = token.rpartition(".")
    secret = _get_session_secret()
    expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None, None, None   # bad signature - tampered with, or signed under an old/different secret
    try:
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
        parts = payload.split("|")
        if len(parts) == 4:
            username, expires_str, ws_part, trial_part = parts
        elif len(parts) == 3:
            # Backward compat: a token issued before trial_start was added
            # to the payload.
            username, expires_str, ws_part = parts
            trial_part = ""
        elif len(parts) == 2:
            # Backward compat: a token issued before workspace_id was added
            # to the payload (old format was "username|expires").
            username, expires_str = parts
            ws_part = ""
            trial_part = ""
        else:
            return None, None, None
        expires = int(expires_str)
    except Exception:
        return None, None, None
    if time.time() > expires:
        return None, None, None
    trial_start = float(trial_part) if trial_part else None
    return username, (ws_part or None), trial_start


def destroy_session(token: str):
    """Called on explicit Logout. Tokens are stateless/self-verifying (see
    the block comment above) precisely so login survives a container
    restart — which means there's no server-side record left to delete.
    The actual logout happens by clearing the browser cookie (see app.py:
    _clear_session_cookie right after this is called); kept as a function
    so every existing call site keeps working unchanged."""
    return


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


# ==================================================================================
# SHARED-DEMO PER-PERSON IDENTITY
# ----------------------------------------------------------------------------------
# A shared demo login (e.g. username "demo" / password "demo123", handed out
# to every prospective client) used to give EVERY separate login a brand-new
# random temporary workspace - so re-opening the app the next day looked like
# all your data got wiped, even though nothing was actually deleted (the old
# workspace was just orphaned, with nothing left pointing at it).
#
# Fix: at login, a demo visitor also gives an email or phone number. That
# identifier is looked up here - first time, it gets a fresh workspace_id and
# its OWN trial_start (a real independent TRIAL_DAYS-day clock, not shared
# with anyone else using the same demo credentials); every later login with
# the SAME identifier gets back the SAME workspace_id and the SAME
# trial_start, so their data and their remaining trial days are exactly
# where they left them - on any browser/device, since this is keyed by
# identifier rather than a browser cookie.
#
# Persisted the same way as credentials.json - local disk as a fast mirror,
# Supabase (if configured) as the copy that survives a container rebuild.
# ==================================================================================
DEMO_IDENTITIES_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                     "demo_identities.json")
DEMO_IDENTITIES_BLOB_KEY = "demo_identities.json"


def _load_demo_identities() -> dict:
    blob = supabase_store.get_blob(DEMO_IDENTITIES_BLOB_KEY)
    if blob:
        try:
            data = json.loads(blob.decode("utf-8"))
            try:
                with open(DEMO_IDENTITIES_FILE, "w") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass
            return data
        except Exception:
            pass
    if not os.path.exists(DEMO_IDENTITIES_FILE):
        return {}
    try:
        with open(DEMO_IDENTITIES_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_demo_identities(data: dict):
    try:
        with open(DEMO_IDENTITIES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass
    try:
        supabase_store.put_blob(DEMO_IDENTITIES_BLOB_KEY, json.dumps(data, indent=2).encode("utf-8"))
    except Exception:
        pass


def _normalize_identifier(identifier: str) -> str:
    return (identifier or "").strip().lower()


def get_or_create_demo_identity(base_username: str, identifier: str) -> dict:
    """base_username: the shared demo account's own username (e.g. "demo").
    identifier: whatever the visitor typed - email or phone number - used
    purely to recognize the SAME person coming back, never as a real login
    credential (the password stays the shared demo123 for everyone).

    Returns {"workspace_id": str, "trial_start": float}. First call for a
    given (base_username, identifier) pair creates a new isolated workspace
    and starts that person's own TRIAL_DAYS-day clock; every later call with
    the same pair returns exactly the same values, so their data and trial
    countdown are stable across logins, browsers, and devices.

    NOTE: this is identity by "what they typed", not a verified email/phone
    (no OTP/confirmation link) - it stops the day-to-day "my data disappeared"
    problem, but a visitor who deliberately types a different email each time
    can still get a fresh trial. Fine for a low-stakes demo funnel; if trial
    abuse ever becomes a real problem, add email/OTP verification on top of
    this same identity record without changing its shape."""
    key = f"{(base_username or '').strip()}:{_normalize_identifier(identifier)}"
    data = _load_demo_identities()
    record = data.get(key)
    if record and record.get("workspace_id"):
        return record
    import uuid
    record = {
        "workspace_id": f"demo_{uuid.uuid4().hex[:12]}",
        "trial_start": time.time(),
        "identifier": _normalize_identifier(identifier),
        "base_username": (base_username or "").strip(),
    }
    data[key] = record
    _save_demo_identities(data)
    return record
