"""
payments.py
-----------
A REAL (not fake/auto-trust) manual payment flow for before you have a proper
payment gateway (Razorpay etc.) set up:

  1. Admin sets a UPI ID + Monthly/Yearly price once (Admin Panel).
  2. A free-plan user picks Monthly or Yearly on the Plans page, pays via
     GPay/PhonePe/any UPI app to that UPI ID, then submits the UPI
     transaction reference number (UTR) they got as proof.
  3. That creates a PENDING request — nobody is upgraded yet, no matter what
     they typed in as the UTR.
  4. Admin sees pending requests in the Admin Panel, checks the UTR actually
     shows up in their own GPay/bank statement for the right amount, and
     ONLY THEN clicks Approve — that is the one and only thing that flips
     the account to Standard. Reject is also available (e.g. duplicate/fake
     UTR, wrong amount, never arrived).

This is intentionally NOT automatic — a personal UPI ID has no API to verify
payments against, so a typed-in UTR is just a claim, not proof. Nothing in
this module ever trusts that claim by itself:
  - Approval always requires an explicit admin click (decide_request), never
    happens on submission.
  - The exact same UTR can't be submitted twice (case/whitespace-insensitive),
    so a client can't reuse an old, already-seen transaction ID (from a past
    payment of their own, or one they saw/guessed) to fish for a free
    approval — it's rejected at submission time, before it ever reaches the
    admin queue.
  - A user can't stack multiple pending requests to spam the queue — only
    one PENDING request per user at a time.
  - Obviously-fake-looking references (too short, or not the alphanumeric
    shape a real UPI UTR/RRN has) are flagged (not blocked) so the admin
    sees a "⚠️" hint right next to it in the queue, instead of it looking
    identical to a legitimate 12-digit UTR.
  - If an admin ever approves one by mistake (e.g. only realized later that
    the amount didn't actually match), reverse_decision() exists to
    immediately undo the upgrade — see the Admin Panel's "Past decisions"
    tab.
Once a proper gateway (Razorpay etc.) is wired up later, THAT flow can be
fully automatic (signature-verified) — this module is the honest stopgap
before that, and the human check-and-approve step is what makes it honest.
"""

import json
import os
import re
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STORE_DIR = os.path.join(APP_DIR, "workspace_state")
_CONFIG_FILE = os.path.join(_STORE_DIR, "_payment_config.json")
_REQUESTS_FILE = os.path.join(_STORE_DIR, "_payment_requests.json")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_REVERSED = "reversed"   # an approval that was later undone by the admin (see reverse_decision)

PLAN_MONTHLY = "monthly"
PLAN_YEARLY = "yearly"
PLAN_DURATION_DAYS = {PLAN_MONTHLY: 30, PLAN_YEARLY: 365}

# A real UPI transaction reference / RRN is normally a 10-22 character
# alphanumeric string (banks vary a bit). This is only used to FLAG obviously
# implausible input for the admin's attention (e.g. "1234" or "test") — it
# never blocks submission and it is NEVER used to auto-approve anything.
_PLAUSIBLE_UTR_RE = re.compile(r"^[A-Za-z0-9]{8,30}$")


# ---------------------------------------------------------------------------
# CONFIG — the admin's UPI ID + Monthly/Yearly price (one shared config, not per-user)
# ---------------------------------------------------------------------------
def _default_config() -> dict:
    return {"upi_id": "", "payee_name": "", "monthly_price": 299.0, "yearly_price": 2999.0}


def get_config() -> dict:
    if not os.path.isfile(_CONFIG_FILE):
        return _default_config()
    try:
        with open(_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        defaults = _default_config()
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return _default_config()


def set_config(upi_id: str, payee_name: str, monthly_price: float, yearly_price: float):
    os.makedirs(_STORE_DIR, exist_ok=True)
    cfg = {"upi_id": (upi_id or "").strip(), "payee_name": (payee_name or "").strip(),
           "monthly_price": float(monthly_price), "yearly_price": float(yearly_price)}
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f)
    os.replace(tmp, _CONFIG_FILE)


def build_upi_link(amount: float, note: str = "") -> str:
    """Standard UPI deep link. On a phone, tapping/scanning this opens
    WHICHEVER UPI app the user has (GPay, PhonePe, Paytm, etc.) with the
    payee, amount and a note pre-filled — nothing GPay-specific about the
    link itself, GPay is just one of many apps that can open it. On a
    desktop/laptop there's no UPI app to hand it to, which is why the Plans
    page also shows a QR code (scan it with a phone) right next to this
    button."""
    cfg = get_config()
    if not cfg["upi_id"]:
        return ""
    params = {
        "pa": cfg["upi_id"],
        "pn": cfg["payee_name"] or "Payment",
        "am": f"{amount:.2f}",
        "cu": "INR",
    }
    if note:
        params["tn"] = note
    return "upi://pay?" + urllib.parse.urlencode(params)


def qr_png_bytes(upi_link: str):
    """Returns PNG bytes of a scannable QR for the given UPI link, or None if
    the qrcode package isn't installed / link is empty (caller should fall
    back to just showing the link/button — QR is a nice-to-have, not required)."""
    if not upi_link:
        return None
    try:
        import qrcode
        import io
        img = qrcode.make(upi_link)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# REQUEST QUEUE
# ---------------------------------------------------------------------------
def _load_requests():
    if not os.path.isfile(_REQUESTS_FILE):
        return []
    try:
        with open(_REQUESTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _save_requests(requests_list):
    os.makedirs(_STORE_DIR, exist_ok=True)
    tmp = _REQUESTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(requests_list, f)
    os.replace(tmp, _REQUESTS_FILE)


def _utr_format_flag(utr: str):
    """Purely informational heads-up shown next to the UTR in the admin
    queue — never blocks submission, never affects approval. A short/odd
    string isn't PROOF of fraud (real refs vary by bank), but it's exactly
    the kind of thing worth a second look before approving."""
    if len(utr) < 8:
        return "Looks short for a real UPI reference — double-check this one."
    if not _PLAUSIBLE_UTR_RE.match(utr):
        return "Contains characters a UPI UTR/RRN wouldn't normally have — double-check this one."
    return None


def submit_request(username: str, workspace_id: str, utr: str, amount: float,
                    plan_type: str = PLAN_MONTHLY) -> tuple:
    """User submits proof-of-payment. Returns (ok, message). This ONLY ever
    creates a PENDING request — it never upgrades anyone by itself, no
    matter what string is typed in as the UTR. Blocks:
      - an empty UTR
      - a UTR that has already been submitted before (by ANYONE, ever,
        approved/rejected/pending) — so a client can't replay an old
        transaction ID (their own from a previous cycle, or one they saw
        somewhere) to try to get a second free approval
      - a second PENDING request from the same user (edit/wait for the
        first to be decided instead of piling up)
    """
    utr = (utr or "").strip()
    if not utr:
        return False, "Please enter your UPI transaction reference (UTR) number."
    if plan_type not in PLAN_DURATION_DAYS:
        plan_type = PLAN_MONTHLY
    reqs = _load_requests()
    if any(r["utr"].lower() == utr.lower() for r in reqs):
        return False, ("This transaction reference has already been submitted before — each UTR can "
                        "only be used once. If you believe this is a mistake, contact your admin directly.")
    if any(r["username"] == username and r["status"] == STATUS_PENDING for r in reqs):
        return False, "You already have a pending request awaiting approval — please wait for it to be reviewed."
    reqs.append({
        "id": f"{int(time.time())}_{username}",
        "username": username, "workspace_id": workspace_id,
        "utr": utr, "amount": amount, "plan_type": plan_type, "status": STATUS_PENDING,
        "format_flag": _utr_format_flag(utr),
        "submitted_at": time.time(), "decided_at": None,
    })
    _save_requests(reqs)
    return True, ("Request submitted — an admin will check it against their own UPI/bank statement and "
                  "activate your Standard plan once it's confirmed. This isn't instant on purpose.")


def list_requests(status: str = None):
    reqs = _load_requests()
    reqs.sort(key=lambda r: r["submitted_at"], reverse=True)
    if status:
        reqs = [r for r in reqs if r["status"] == status]
    return reqs


def decide_request(request_id: str, approve: bool):
    """Admin action — the ONLY place a request can ever move off PENDING.
    Returns (ok, message, username-or-None, plan_type-or-None). Caller
    (app.py) is responsible for actually calling
    auth.set_plan(username, 'standard', ...) when approve=True — kept
    separate so this module has no dependency on auth.py."""
    reqs = _load_requests()
    for r in reqs:
        if r["id"] == request_id:
            if r["status"] != STATUS_PENDING:
                return False, "This request was already decided.", None, None
            r["status"] = STATUS_APPROVED if approve else STATUS_REJECTED
            r["decided_at"] = time.time()
            _save_requests(reqs)
            return True, ("Approved." if approve else "Rejected."), r["username"], r.get("plan_type", PLAN_MONTHLY)
    return False, "Request not found.", None, None


def reverse_decision(request_id: str):
    """Admin action to undo a mistaken Approve (e.g. the amount turned out
    not to actually match). Only works on a currently-APPROVED request.
    Returns (ok, message, username-or-None). Caller (app.py) is responsible
    for actually calling auth.set_plan(username, 'free')."""
    reqs = _load_requests()
    for r in reqs:
        if r["id"] == request_id:
            if r["status"] != STATUS_APPROVED:
                return False, "Only an approved request can be reversed.", None
            r["status"] = STATUS_REVERSED
            r["decided_at"] = time.time()
            _save_requests(reqs)
            return True, "This approval has been reversed.", r["username"]
    return False, "Request not found.", None
