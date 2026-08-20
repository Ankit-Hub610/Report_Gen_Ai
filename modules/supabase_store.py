"""
supabase_store.py
------------------
Generic persistent key/value BLOB store backed by Supabase (Postgres, via its
auto-generated REST API / PostgREST) - used so that things which used to live
ONLY on local disk (workspace_state/) actually survive a container
rebuild/redeploy on hosts with an ephemeral filesystem (Streamlit Community
Cloud sleeps after inactivity and rebuilds fresh from GitHub - anything only
ever written to local disk at runtime is gone when that happens).

This mirrors the exact same "best-effort, local-disk-first-if-unconfigured"
pattern already used for branding's GitHub-Contents-API sync in
workspace_store.py, generalized to ANY blob (credentials.json, branding,
per-workspace dashboard state, demo-client identity records...), so all of
those now survive a restart the same way branding already did.

SETUP (one-time, in the Supabase project's SQL editor):

    create table if not exists app_kv (
        key text primary key,
        value text not null,
        updated_at timestamptz not null default now()
    );

Then, in Streamlit Settings -> Secrets (or a local .streamlit/secrets.toml):

    SUPABASE_URL = "https://xxxxxxxx.supabase.co"
    SUPABASE_SERVICE_KEY = "sb_secret_...."   # Project Settings -> API Keys.
                                        # Use the SECRET key (new key system,
                                        # starts "sb_secret_") or, on an
                                        # older project, the legacy
                                        # SERVICE ROLE key (starts "eyJ...") -
                                        # NOT the publishable/anon key. This
                                        # key bypasses Row Level Security,
                                        # which is what lets the app
                                        # read/write app_kv without setting up
                                        # RLS policies. Keep this secret - it
                                        # has full read/write access to the DB.

Without those two secrets set, every function here is a silent no-op /
returns None, and callers fall back to local disk exactly like before this
module existed - nothing breaks for an install that hasn't set up Supabase.
"""

import base64
import os

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

TABLE = "app_kv"


def _config():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    try:
        import streamlit as st
        url = st.secrets.get("SUPABASE_URL", url)
        key = st.secrets.get("SUPABASE_SERVICE_KEY", key)
    except Exception:
        pass
    if url and key:
        return {"url": url.rstrip("/"), "key": key}
    return None


def available() -> bool:
    """True if Supabase persistence is configured and usable. Callers can use
    this to warn admins/users when durable storage isn't set up yet."""
    return REQUESTS_AVAILABLE and _config() is not None


def _headers(cfg):
    """Supabase now has TWO API key formats (as of 2026):
      - Legacy JWT keys (service_role/anon) — long strings starting "eyJ",
        which need BOTH the `apikey` header and `Authorization: Bearer`.
      - New keys (sb_secret_... / sb_publishable_...) — short opaque tokens
        that must go ONLY on the `apikey` header. Sending one on
        `Authorization: Bearer` too gets the request rejected ("not a
        JWT") — Supabase's own docs are explicit about this.
    So: always send apikey; only add Authorization for a legacy JWT-looking
    key, never for a new-format sb_... key."""
    headers = {"apikey": cfg["key"], "Content-Type": "application/json"}
    if not cfg["key"].startswith("sb_"):
        headers["Authorization"] = f"Bearer {cfg['key']}"
    return headers


def get_blob(key: str):
    """Returns the raw bytes stored under `key`, or None if missing/
    unreachable/not configured. Never raises."""
    cfg = _config()
    if not cfg or not REQUESTS_AVAILABLE:
        return None
    try:
        r = requests.get(
            f"{cfg['url']}/rest/v1/{TABLE}",
            headers=_headers(cfg),
            params={"key": f"eq.{key}", "select": "value"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        rows = r.json()
        if not rows:
            return None
        return base64.b64decode(rows[0]["value"])
    except Exception:
        return None


def put_blob(key: str, data: bytes) -> bool:
    """Upserts `data` under `key`. Best-effort - returns True/False, never
    raises, and a failure here should never block the caller's local-disk
    save from also happening."""
    cfg = _config()
    if not cfg or not REQUESTS_AVAILABLE:
        return False
    try:
        payload = {"key": key, "value": base64.b64encode(data).decode("ascii")}
        r = requests.post(
            f"{cfg['url']}/rest/v1/{TABLE}",
            headers={**_headers(cfg), "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=payload,
            timeout=10,
        )
        return r.status_code in (200, 201, 204)
    except Exception:
        return False


def delete_blob(key: str) -> bool:
    cfg = _config()
    if not cfg or not REQUESTS_AVAILABLE:
        return False
    try:
        r = requests.delete(
            f"{cfg['url']}/rest/v1/{TABLE}",
            headers=_headers(cfg),
            params={"key": f"eq.{key}"},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False
