"""
email_service.py
-----------------
Sends the "Forgot password" reset-link email using Resend
(https://resend.com), which has a genuinely free tier (100 emails/day,
no credit card) and, unlike most providers, lets you send FROM their
shared test address (onboarding@resend.dev) TO any real inbox without
verifying your own domain first — so this works out of the box.

SETUP (one-time, free):
  1. Go to https://resend.com -> sign up (free, no card) -> API Keys -> Create key.
  2. Either:
       a) set environment variable RESEND_API_KEY before running streamlit, OR
       b) create .streamlit/secrets.toml with:  RESEND_API_KEY = "re_..."
     If neither is set, "Forgot password" will tell the admin it isn't
     configured yet instead of pretending to send an email.

(Optional, later) If you want the email to say "from
yourcompany.com" instead of Resend's shared address, verify your own
domain in the Resend dashboard and change SENDER_EMAIL below.
"""

import os

import requests

RESEND_URL = "https://api.resend.com/emails"
SENDER_EMAIL = "🔐 RA-Intelligence Platform <onboarding@resend.dev>"


def get_api_key():
    key = os.environ.get("RESEND_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        if "RESEND_API_KEY" in st.secrets:
            return st.secrets["RESEND_API_KEY"]
    except Exception:
        pass
    return None


def send_password_reset_email(to_email: str, username: str, reset_url: str) -> tuple[bool, str]:
    """Returns (success, message)."""
    api_key = get_api_key()
    if not api_key:
        return False, ("Email sending isn't configured yet — ask your admin to set up a free "
                        "Resend API key (see README).")

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
    try:
        resp = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": SENDER_EMAIL, "to": [to_email],
                  "subject": "Reset your password — RA-Intelligence Platform", "html": html},
            timeout=20,
        )
        if resp.status_code >= 400:
            return False, f"Email provider error ({resp.status_code}): {resp.text[:200]}"
        return True, "Sent."
    except Exception as e:
        return False, f"Network error sending email: {e}"


def send_report_email(to_email: str, subject: str, body_markdown: str) -> tuple[bool, str]:
    """Sends any plain/markdown-ish text body as a simple HTML email — used by
    the 🧠 Intelligence Report page's 'Email this report' button. Returns
    (success, message)."""
    api_key = get_api_key()
    if not api_key:
        return False, ("Email sending isn't configured yet — ask your admin to set up a free "
                        "Resend API key (see README).")

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
    try:
        resp = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"from": SENDER_EMAIL, "to": [to_email], "subject": subject, "html": html},
            timeout=20,
        )
        if resp.status_code >= 400:
            return False, f"Email provider error ({resp.status_code}): {resp.text[:200]}"
        return True, "Sent."
    except Exception as e:
        return False, f"Network error sending email: {e}"
