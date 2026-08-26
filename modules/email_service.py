"""
email_service.py
-----------------
Sends the "Forgot password" reset-link email via either of two methods —
pick whichever is set up (Gmail is checked first):

  1. GMAIL SMTP (recommended if you don't own a domain yet) — completely
     free, no domain needed, delivers to ANY real inbox immediately.
     Limit: 500 recipients/day on a normal Gmail account, which is far
     more than a small app's password-reset volume needs.

     SETUP (5 minutes, free):
       a) On the Gmail account you want to send FROM, turn on 2-Step
          Verification: https://myaccount.google.com/security
       b) Then create an App Password: https://myaccount.google.com/apppasswords
          (pick "Mail" as the app). Google shows you a 16-character code —
          copy it (spaces don't matter).
       c) Set these two secrets (in .streamlit/secrets.toml or as env vars):
            GMAIL_ADDRESS = "youraddress@gmail.com"
            GMAIL_APP_PASSWORD = "the 16-character code from step b"
       That's it — no domain, no DNS, works right away, sends to anyone.

  2. RESEND (https://resend.com) — a genuinely free tier (100 emails/day,
     no credit card), but its shared sandbox sender (onboarding@resend.dev)
     can ONLY deliver to the single email address you signed up to Resend
     with, until you verify a domain you own. That's a Resend anti-abuse
     restriction, not a limit of this app — it's why, without a verified
     domain, a client typing their own email still sees the reset link
     land in the admin's inbox instead. Use this path once you own a
     domain and want branded "from yourcompany.com" emails; until then,
     Gmail (above) is the simpler free option.

     SETUP:
       a) https://resend.com -> sign up (free, no card) -> API Keys -> Create key.
       b) Set RESEND_API_KEY (env var or secrets.toml).
       c) To deliver to real client inboxes (not just the admin's): verify
          a domain in Resend -> Domains -> Add Domain, add the DNS records
          Resend shows you at your domain registrar, wait for "Verified"
          (minutes, sometimes up to ~48h), then set RESEND_SENDER_EMAIL to
          an address on that domain, e.g. "RA-Intelligence <noreply@yourdomain.com>".

If NEITHER Gmail nor Resend credentials are set, "Forgot password" tells
the admin it isn't configured yet instead of pretending to send an email.
"""

import os
import smtplib
from email.mime.text import MIMEText

import requests

RESEND_URL = "https://api.resend.com/emails"

# Sandbox default — restricted to the admin's own Resend signup email
# until a domain is verified (see the module docstring above). Set
# RESEND_SENDER_EMAIL once a domain is verified to lift that restriction.
SENDER_EMAIL = os.environ.get("RESEND_SENDER_EMAIL") or "🔐 RA-Intelligence Platform <onboarding@resend.dev>"


def _get_secret(name: str):
    """Checks the environment variable first, then st.secrets — same
    fallback pattern used throughout this file."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return None


def _get_sender_email():
    return _get_secret("RESEND_SENDER_EMAIL") or SENDER_EMAIL


def get_api_key():
    return _get_secret("RESEND_API_KEY")


def _gmail_credentials():
    """Returns (address, app_password) if both Gmail secrets are set,
    else None — used to decide whether Gmail SMTP is available at all."""
    address = _get_secret("GMAIL_ADDRESS")
    app_password = _get_secret("GMAIL_APP_PASSWORD")
    if address and app_password:
        return address, app_password
    return None


def _send_via_gmail(to_email: str, subject: str, html: str):
    """Returns (success, message). Sends through Gmail's SMTP server using
    an App Password — no domain required, works immediately, delivers to
    any real inbox (see the Gmail setup steps in the module docstring)."""
    creds = _gmail_credentials()
    if not creds:
        return None  # Gmail isn't configured — caller falls back to Resend
    address, app_password = creds
    try:
        msg = MIMEText(html, "html")
        msg["Subject"] = subject
        msg["From"] = f"RA-Intelligence <{address}>"
        msg["To"] = to_email
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(address, app_password)
            server.sendmail(address, [to_email], msg.as_string())
        return True, "Sent."
    except smtplib.SMTPAuthenticationError:
        return False, ("Gmail rejected the login — double-check GMAIL_APP_PASSWORD is a 16-character "
                        "App Password (not your normal Gmail password), and that 2-Step Verification "
                        "is on for that Gmail account.")
    except Exception as e:
        return False, f"Gmail SMTP error: {e}"


def send_password_reset_email(to_email: str, username: str, reset_url: str) -> tuple[bool, str]:
    """Returns (success, message). Tries Gmail SMTP first (if configured —
    no domain needed, see module docstring), then falls back to Resend."""
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:auto;">
      <h2>🔑 RA-Intelligence </h2>
      <p>Hi {username},</p>
      <p>We got a request to reset the password on your account. Click the button
      below to set a new one — this link works once and expires in 30 minutes.</p>
      <p style="text-align:center;margin:32px 0;">
        <a href="{reset_url}" style="background:#e53e3e;color:white;padding:12px 24px;
           border-radius:6px;text-decoration:none;font-weight:bold;">Create new password</a>
      </p>
      <p style="color:#666;font-size:13px;">If you didn't request this, you can safely ignore this
      email — your password won't change unless you click the link above.</p>
    </div>
    """
    subject = "Reset your password — RA-Intelligence Platform"

    gmail_result = _send_via_gmail(to_email, subject, html)
    if gmail_result is not None:
        return gmail_result  # Gmail was configured — use its result either way, don't also try Resend

    api_key = get_api_key()
    if not api_key:
        return False, ("Email sending isn't configured yet — ask your admin to set up either a free "
                        "Gmail App Password or a Resend API key (see modules/email_service.py).")

    try:
        resp = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": _get_sender_email(), "to": [to_email],
                  "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code == 403 and "resend.dev" in _get_sender_email() and "only send" in resp.text.lower():
            # This exact 403 means: still on the sandbox sender, and to_email
            # isn't the Resend account's own signup email - Resend refuses to
            # deliver it, by design. Verifying a domain is the only fix (see
            # the module docstring at the top of this file).
            return False, ("This app is still using Resend's free sandbox sender, which can only "
                            "deliver to the admin's own Resend signup email - not to clients' real "
                            "inboxes yet. Ask your admin to verify a domain in Resend and set "
                            "RESEND_SENDER_EMAIL (see modules/email_service.py for steps).")
        if resp.status_code >= 400:
            return False, f"Email provider error ({resp.status_code}): {resp.text[:200]}"
        return True, "Sent."
    except Exception as e:
        return False, f"Network error sending email: {e}"


def send_report_email(to_email: str, subject: str, body_markdown: str) -> tuple[bool, str]:
    """Sends any plain/markdown-ish text body as a simple HTML email — used by
    the 🧠 Intelligence Report page's 'Email this report' button. Returns
    (success, message). Tries Gmail SMTP first (if configured), then Resend."""
    # Very light markdown-ish -> HTML: headers and line breaks only, good enough for a report body.
    safe = (body_markdown or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_lines = []
    for line in safe.splitlines():
        stripped = line.strip()
        if stripped.startswith("### "):
            html_lines.append(f"<h3>{stripped[4:]}</h3>")
        elif stripped.startswith("## "):
            html_lines.append(f"<h2>{stripped[3:]}</h2>")
        elif stripped.startswith("# "):
            html_lines.append(f"<h1>{stripped[2:]}</h1>")
        elif stripped == "":
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p>{stripped}</p>")
    html = f"""
    <div style="font-family:sans-serif;max-width:680px;margin:auto;">
      <h2>🧠 Intelligence Report — RA-Intelligence Platform</h2>
      {''.join(html_lines)}
    </div>
    """

    gmail_result = _send_via_gmail(to_email, subject, html)
    if gmail_result is not None:
        return gmail_result

    api_key = get_api_key()
    if not api_key:
        return False, ("Email sending isn't configured yet — ask your admin to set up either a free "
                        "Gmail App Password or a Resend API key (see modules/email_service.py).")
    try:
        resp = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": _get_sender_email(), "to": [to_email], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 400:
            return False, f"Email provider error ({resp.status_code}): {resp.text[:200]}"
        return True, "Sent."
    except Exception as e:
        return False, f"Network error sending email: {e}"
