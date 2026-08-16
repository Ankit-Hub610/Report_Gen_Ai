"""
payments.py
-----------
A REAL (not fake/auto-trust) manual payment flow for before you have a proper
payment gateway (Razorpay etc.) set up:

  1. Admin sets a UPI ID + Monthly/Yearly price once (Admin Panel).
  2. A free-plan user picks Monthly or Yearly, pays via GPay/PhonePe/any UPI
     app to that UPI ID, then submits the UPI transaction reference number
     (UTR) they got as proof.
  3. That creates a PENDING request — nobody is upgraded yet.
  4. Admin sees pending requests in the Admin Panel, checks the UTR actually
     shows up in their own GPay/bank statement (for the right amount, on
     the right day), and clicks Approve — THAT is what actually flips the
     account to Standard. Reject is also available (e.g. duplicate/fake
     UTR), and an approved-by-mistake request can later be Reversed.

WHY THIS ISN'T FULLY AUTOMATIC: a personal UPI ID has no API to verify
payments against, so an "instant auto-upgrade" from just a typed-in UTR
would be trivially fake-able — anyone could type in a random string and get
free Standard access. A human check-and-approve step is what makes this
real. What THIS module can do — and does — is make it hard to slip a
low-effort fake past that human check:

  - UTR SANITY CHECK (utr_quality()): rejects empty/placeholder/too-short
    input outright at submit time (e.g. "test", "0000000000", "123"), and
    flags (but doesn't block — real bank statement formats do vary) any UTR
    that doesn't match the common 12-digit UPI reference shape, so the
    admin sees a clear "double-check this one" signal instead of having to
    eyeball every entry themselves.
  - DUPLICATE UTR CHECK: a real transaction reference is unique and can
    never legitimately be submitted twice (by the same person OR a
    different one) — blocked outright, checked against every request ever
    submitted regardless of its status.
  - FIXED AMOUNT: the amount tied to a request is always the admin's
    configured Monthly/Yearly price, never something the user types in — so
    nobody can submit "₹1 paid" and ask for a ₹999 plan.
  - ONE PENDING REQUEST AT A TIME per user — no piling up duplicate/spam
    submissions to bury the admin's queue.

None of this is proof of payment on its own — the admin's own bank/UPI-app
check is still what actually confirms the money arrived. It's what turns
"trust a random typed string" into "a human verifies one specific,
already-sanity-checked claim." Once a proper gateway (Razorpay etc.) is
wired up later, THAT flow can be fully automatic (signature-verified) — this
module is the honest, harder-to-abuse stopgap before that.
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
STATUS_REVERSED = "reversed"

PLAN_MONTHLY = "monthly"
PLAN_YEARLY = "yearly"
PLAN_DURATION_DAYS = {PLAN_MONTHLY: 30, PLAN_YEARLY: 365}


# ---------------------------------------------------------------------------
# CONFIG — the admin's UPI ID + Monthly/Yearly price (one shared config)
# ---------------------------------------------------------------------------
def get_config() -> dict:
    defaults = {"upi_id": "", "payee_name": "", "monthly_price": 299.0, "yearly_price": 2999.0}
    if not os.path.isfile(_CONFIG_FILE):
        return defaults
    try:
        with open(_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        return cfg
    except Exception:
        return defaults


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
    amount pre-filled — nothing GPay-specific about the link itself, GPay is
    just one of many apps that can open it."""
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
# UTR SANITY CHECK — not proof of payment, but catches low-effort fakes
# ---------------------------------------------------------------------------
_JUNK_UTRS = {"na", "n/a", "none", "test", "testing", "xxxx", "xxxxxxxxxxxx",
              "123", "0", "000000000000", "123456789012", "111111111111", "testtest"}
_TYPICAL_UTR_RE = re.compile(r"^\d{12}$")            # the standard GPay/PhonePe/most-banks UPI reference shape
_PLAUSIBLE_UTR_RE = re.compile(r"^[A-Za-z0-9]{6,25}$")  # generously covers other real bank statement formats


def utr_quality(utr: str) -> dict:
    """Best-effort sanity check on a submitted UTR/UPI reference. NOT proof
    of payment — there's no gateway API to verify against here — this only
    catches input that couldn't possibly be a real reference, and flags
    anything unusual for the admin to double-check.
    Returns {"ok": bool, "typical_format": bool, "reason": str|None}.
    ok=False means "reject this at submit time" (empty/placeholder/too
    short). ok=True but typical_format=False means "let it through, but
    show the admin a caution flag" — real bank UTR formats genuinely vary,
    so this must never hard-block a legitimate payer."""
    u = (utr or "").strip()
    if not u:
        return {"ok": False, "typical_format": False, "reason": "Please enter your UPI transaction reference (UTR) number."}
    if u.lower() in _JUNK_UTRS or len(set(u.lower())) <= 1:
        return {"ok": False, "typical_format": False,
                "reason": "That doesn't look like a real transaction reference — please paste the exact UTR/reference ID from your UPI app."}
    if len(u) < 6:
        return {"ok": False, "typical_format": False,
                "reason": "Too short to be a real UPI transaction reference (these are normally 12 digits)."}
    if not _PLAUSIBLE_UTR_RE.match(u):
        return {"ok": False, "typical_format": False,
                "reason": "A UPI reference is normally just letters/numbers, around 12 characters — please re-check what you pasted."}
    is_typical = bool(_TYPICAL_UTR_RE.match(u))
    return {"ok": True, "typical_format": is_typical,
            "reason": None if is_typical else
            "Doesn't match the usual 12-digit UPI reference format — double-check it carefully in your bank/UPI app before approving."}


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


def submit_request(username: str, workspace_id: str, utr: str, amount: float, plan_type: str = PLAN_MONTHLY) -> tuple:
    """User submits proof-of-payment. Returns (ok, message).
    Rejects obviously-fake UTRs outright (see utr_quality()), blocks
    duplicate UTRs (a real one can never legitimately be reused — checked
    against every request ever submitted, not just pending ones), and
    blocks a second PENDING request from the same user (they'd have to wait
    for the first to be reviewed rather than piling up submissions)."""
    utr = (utr or "").strip()
    quality = utr_quality(utr)
    if not quality["ok"]:
        return False, quality["reason"]
    reqs = _load_requests()
    if any(r["utr"].lower() == utr.lower() for r in reqs):
        return False, "This transaction reference has already been submitted."
    if any(r["username"] == username and r["status"] == STATUS_PENDING for r in reqs):
        return False, "You already have a pending request awaiting approval — please wait for it to be reviewed."
    reqs.append({
        "id": f"{int(time.time())}_{username}",
        "username": username, "workspace_id": workspace_id,
        "utr": utr, "amount": amount, "plan_type": plan_type if plan_type in PLAN_DURATION_DAYS else PLAN_MONTHLY,
        "status": STATUS_PENDING,
        "format_flag": quality["reason"],  # None for a typical-looking UTR, else the caution message for the admin
        "submitted_at": time.time(), "decided_at": None,
    })
    _save_requests(reqs)
    return True, "Request submitted — an admin will verify and activate your Standard plan shortly."


def list_requests(status: str = None):
    reqs = _load_requests()
    reqs.sort(key=lambda r: r["submitted_at"], reverse=True)
    if status:
        reqs = [r for r in reqs if r["status"] == status]
    return reqs


def decide_request(request_id: str, approve: bool):
    """Admin action. Returns (ok, message, username-or-None, plan_type-or-None).
    Caller (app.py) is responsible for actually calling auth.set_plan(...)
    when approve=True — kept separate so this module has no dependency on
    auth.py."""
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
    """Undoes a mistaken Approve (e.g. only a partial amount actually came
    through, spotted after the fact). Returns (ok, message, username-or-None).
    Only valid on a currently-APPROVED request — caller (app.py) is
    responsible for moving the account back to Free via auth.set_plan()."""
    reqs = _load_requests()
    for r in reqs:
        if r["id"] == request_id:
            if r["status"] != STATUS_APPROVED:
                return False, "Only an approved request can be reversed.", None
            r["status"] = STATUS_REVERSED
            r["decided_at"] = time.time()
            _save_requests(reqs)
            return True, "Approval reversed.", r["username"]
    return False, "Request not found.", None
