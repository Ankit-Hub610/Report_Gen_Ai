"""
usage_limits.py
----------------
Lightweight daily usage caps for accounts on the "free" plan (see auth.plan).
Accounts on the "standard" plan (the default for every existing account, so
nobody gets newly capped by surprise) are always unlimited.

Why this exists: before opening the tool up to a wide/anonymous audience (a
public "try the demo" link, a generic launch, etc.), a handful of costly or
abusable actions need a ceiling — otherwise one over-enthusiastic tester can
burn the whole AI quota or hammer PDF generation for everyone else.

Deliberately simple: counts are kept in ONE small JSON file (not a database),
reset automatically at UTC midnight, and every check is best-effort — a
storage hiccup here should never block a paying/standard user, so failures
fail OPEN (i.e. allow the action) rather than closed.
"""

import json
import os
import threading

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_USAGE_FILE = os.path.join(APP_DIR, "workspace_state", "_usage_limits.json")
_LOCK = threading.Lock()

# Caps for the "free" plan. Adjust freely — these are intentionally generous
# enough to let someone genuinely evaluate the tool, but not enough to be
# used as a permanent free substitute for a paid account.
FREE_PLAN_LIMITS = {
    "ai_calls": 15,        # AI Assistant questions + AI report write-ups, combined, per day
    "pdf_exports": 5,      # Boss Dashboard PDF exports per day
    "max_rows": 5000,      # rows accepted from an uploaded file / DB query
}


def _today_str():
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%d")


def _load():
    if not os.path.isfile(_USAGE_FILE):
        return {}
    try:
        with open(_USAGE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(_USAGE_FILE), exist_ok=True)
        tmp = _USAGE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _USAGE_FILE)
    except Exception:
        pass  # best-effort — a failed write just means today's count resets sooner than intended


def _get_bucket(data, workspace_id):
    today = _today_str()
    bucket = data.get(workspace_id)
    if not bucket or bucket.get("date") != today:
        bucket = {"date": today, "ai_calls": 0, "pdf_exports": 0}
        data[workspace_id] = bucket
    return bucket


def check_and_increment(workspace_id: str, plan: str, kind: str) -> tuple:
    """Call this right BEFORE performing a capped action (kind: 'ai_calls' or
    'pdf_exports'). Returns (allowed: bool, message: str|None). If allowed,
    the count has already been incremented — don't call this speculatively."""
    if plan != "free":
        return True, None
    limit = FREE_PLAN_LIMITS.get(kind)
    if limit is None:
        return True, None
    with _LOCK:
        data = _load()
        bucket = _get_bucket(data, workspace_id)
        used = bucket.get(kind, 0)
        if used >= limit:
            label = {"ai_calls": "AI requests", "pdf_exports": "PDF exports"}.get(kind, kind)
            return False, (f"You've used all {limit} free {label} for today. "
                           f"This resets at midnight UTC, or ask your admin to upgrade your plan.")
        bucket[kind] = used + 1
        data[workspace_id] = bucket
        _save(data)
    return True, None


def remaining(workspace_id: str, plan: str, kind: str):
    """Returns remaining count today, or None if this plan/kind has no cap
    (i.e. don't show a counter in the UI)."""
    if plan != "free":
        return None
    limit = FREE_PLAN_LIMITS.get(kind)
    if limit is None:
        return None
    data = _load()
    bucket = _get_bucket(data, workspace_id)
    return max(0, limit - bucket.get(kind, 0))


def row_limit_for_plan(plan: str):
    """Returns the max rows a 'free' plan may load, or None (no cap) otherwise."""
    return FREE_PLAN_LIMITS["max_rows"] if plan == "free" else None
