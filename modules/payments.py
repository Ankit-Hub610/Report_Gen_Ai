"""
payments.py
-----------
A REAL (not fake/auto-trust) manual payment flow for before you have a proper
payment gateway (Razorpay etc.) set up:

  1. Admin sets a UPI ID + monthly price once (Admin Panel).
  2. A free-plan user sees a "Pay via UPI" button/QR on the Plans page, pays
     via GPay/PhonePe/any UPI app to that UPI ID, then submits the UPI
     transaction reference number (UTR) they got as proof.
  3. That creates a PENDING request — nobody is upgraded yet.
  4. Admin sees pending requests in the Admin Panel, checks the UTR actually
     shows up in their own GPay/bank statement, and clicks Approve — THAT is
     what actually flips the account to Standard. Reject is also available
     (e.g. duplicate/fake UTR).

This is intentionally NOT automatic — a personal UPI ID has no API to verify
payments against, so any "instant auto-upgrade" from just a typed-in UTR would
be trivially fake-able (anyone could type in a random string and get free
Standard access). A human check-and-approve step is what makes this real. Once
a proper gateway (Razorpay etc.) is wired up later, THAT flow can be fully
automatic (signature-verified) — this module is the honest stopgap before that.
"""

import json
import os
import time
import urllib.parse

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STORE_DIR = os.path.join(APP_DIR, "workspace_state")
_CONFIG_FILE = os.path.join(_STORE_DIR, "_payment_config.json")
_REQUESTS_FILE = os.path.join(_STORE_DIR, "_payment_requests.json")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


# ---------------------------------------------------------------------------
# CONFIG — the admin's UPI ID + monthly price (one shared config, not per-user)
# ---------------------------------------------------------------------------
def get_config() -> dict:
    if not os.path.isfile(_CONFIG_FILE):
        return {"upi_id": "", "payee_name": "", "monthly_price": 299}
    try:
        with open(_CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        cfg.setdefault("upi_id", "")
        cfg.setdefault("payee_name", "")
        cfg.setdefault("monthly_price", 299)
        return cfg
    except Exception:
        return {"upi_id": "", "payee_name": "", "monthly_price": 299}


def set_config(upi_id: str, payee_name: str, monthly_price: float):
    os.makedirs(_STORE_DIR, exist_ok=True)
    cfg = {"upi_id": (upi_id or "").strip(), "payee_name": (payee_name or "").strip(),
           "monthly_price": float(monthly_price)}
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


def submit_request(username: str, workspace_id: str, utr: str, amount: float) -> tuple:
    """User submits proof-of-payment. Returns (ok, message). Blocks duplicate
    UTRs (accidental double-submit) and blocks a second PENDING request from
    the same user (edit/cancel the first instead of piling up)."""
    utr = (utr or "").strip()
    if not utr:
        return False, "Please enter your UPI transaction reference (UTR) number."
    reqs = _load_requests()
    if any(r["utr"].lower() == utr.lower() for r in reqs):
        return False, "This transaction reference has already been submitted."
    if any(r["username"] == username and r["status"] == STATUS_PENDING for r in reqs):
        return False, "You already have a pending request awaiting approval — please wait for it to be reviewed."
    reqs.append({
        "id": f"{int(time.time())}_{username}",
        "username": username, "workspace_id": workspace_id,
        "utr": utr, "amount": amount, "status": STATUS_PENDING,
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
    """Admin action. Returns (ok, message, username-or-None). Caller (app.py)
    is responsible for actually calling auth.set_plan(username, 'standard')
    when approve=True — kept separate so this module has no dependency on
    auth.py."""
    reqs = _load_requests()
    for r in reqs:
        if r["id"] == request_id:
            if r["status"] != STATUS_PENDING:
                return False, "This request was already decided.", None
            r["status"] = STATUS_APPROVED if approve else STATUS_REJECTED
            r["decided_at"] = time.time()
            _save_requests(reqs)
            return True, ("Approved." if approve else "Rejected."), r["username"]
    return False, "Request not found.", None
