"""
app.py
------
SPORTS ANALYTICS PLATFORM
A password-protected, self-contained BI tool that works on ANY tabular
dataset (CSV / XLSX / JSON / PDF). No column names are hard-coded anywhere -
everything (KPIs, chart variants, filters) is auto-derived from whatever
data you load.

PAGES
  1. Raw Analysis   -> upload data, auto KPIs, 10 variants per chart family,
                        filters on everything, pick your favourites (⭐)
  2. Boss Dashboard -> only the charts/KPIs you picked, full theme control,
                        swap any chart for another variant, export to PDF
  3. Data Table     -> SQL-like column picker + filters + sort + CSV export
  4. Settings       -> tool defaults, full "How this works" guide
  5. Admin Panel    -> admin-only (separate 🔐 login on the sign-in screen):
                        create/delete report-user accounts, reset passwords

Run with:  streamlit run app.py
"""

import os
import sys
import io
import copy
import hashlib
import time
import base64
import numpy as np
import pandas as pd
import streamlit as st

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules import auth, data_engine as de, chart_engine as ce, pdf_export as pe
from modules import measures as ms, builder_engine as be
from modules import workspace_store as ws
from modules import query_engine as qe
from modules import db_connector as dbc
from modules import ai_chat as ac
from modules import email_service as es

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except ImportError:
    AUTOREFRESH_AVAILABLE = False

# ==================================================================================
# PAGE CONFIG
# ==================================================================================
st.set_page_config(page_title="RA-Intelligence By Ankit_Solanki", page_icon="🕵️‍♀️", layout="wide")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(APP_DIR, "sample_data", "sample_sports_payments.csv")

DEFAULT_THEME = {
    "bg_color": "#0E1117",
    "panel_color": "#161A23",
    "font_color": "#F5F5F5",
    "accent_color": "#2C6E49",
    "font_name": "Helvetica",
    "font_family": "Arial",
    "font_size": 13,
    "palette_name": "Set2",
    "show_legend": True,
    "show_labels": True,
    "template": "plotly_dark",
    "chart_bg": "rgba(0,0,0,0)",
    "plot_bg": "rgba(0,0,0,0)",
    "wallpaper_bytes": None,
}

PALETTES = {
    "Set2": ["#66C2A5", "#FC8D62", "#8DA0CB", "#E78AC3", "#A6D854", "#FFD92F", "#E5C494", "#B3B3B3"],
    "Bold": ["#7F3C8D", "#11A579", "#3969AC", "#F2B701", "#E73F74", "#80BA5A", "#E68310", "#008695"],
    "Corporate Blue": ["#003f5c", "#2f4b7c", "#665191", "#a05195", "#d45087", "#f95d6a", "#ff7c43", "#ffa600"],
    "Vivid": ["#E58606", "#5D69B1", "#52BCA3", "#99C945", "#CC61B0", "#24796C", "#DAA51B", "#2F8AC4"],
    "Mono Green": ["#013220", "#0B6E4F", "#08A045", "#6BCB77", "#A6E3A1", "#D4F1D4"],
}

def _load_asset_bytes(filename: str):
    """Reads a file from the assets/ folder next to app.py (committed to the
    git repo, unlike workspace_state/ which is gitignored runtime data) —
    used so the default logo is baked into the deploy itself and survives
    every redeploy, with no admin action needed. Returns None (never raises)
    if the file isn't there, so a missing asset just means no default logo
    instead of crashing the app."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", filename)
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


DEFAULT_BRAND = {
    "text": "RA-I - Research & Analytics Intelligence",
    "font_size": 22,       # px, sidebar heading
    "color": "#F5F5F5",
    "bold": True,
    "italic": False,
    "font_family": "sans-serif",  # sans-serif / serif / monospace
    # Baked-in default logo (assets/logo_light.png, assets/logo_dark.png — ship these
    # two files in the repo). An admin can still override either from Settings → App
    # Branding; that override is saved to workspace_state/ instead (see save_branding()
    # below) and — unlike these baked-in defaults — won't survive a future redeploy
    # unless workspace_state/ is on persistent storage, so re-upload there if needed.
    "logo_light_bytes": _load_asset_bytes("logo_light.png"),  # shown when the app is in LIGHT theme
    "logo_light_url": "",
    "logo_dark_bytes": _load_asset_bytes("logo_dark.png"),    # shown when the app is in DARK theme
    "logo_dark_url": "",
    "logo_width": 220,     # px — used on the login page; sidebar shows it smaller automatically
    "logo_glow": True,     # pulsing blue glow on the logo's edges
    "hide_login_title": True,  # when a logo is set, replace the "Sports Analytics Platform" text with it on the login page
}

FAMILY_ICONS = {
    "Bar": "📊", "Line": "📈", "Pie": "🥧", "Comparison": "⚖️", "Area": "🏔️",
    "Scatter": "🔵", "Box": "📦", "Histogram": "📶", "Treemap": "🌳", "Heatmap": "🔥",
}


def _img_data_uri(data: bytes) -> str:
    """Turns raw uploaded image bytes into a data: URI so they can go straight
    into a hand-written <img src=...> tag (needed for the glow/theme-swap CSS
    below - st.image() can't carry custom CSS classes)."""
    import base64
    if data[:3] == b"\xff\xd8\xff":
        mime = "image/jpeg"
    else:
        mime = "image/png"  # PNG (and everything else we accept) falls back fine as png
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _brand_logo_srcs(brand: dict):
    """Returns (light_src, dark_src) - each either a data: URI (uploaded file,
    preferred) or a plain URL, or None if nothing is configured for that
    theme. If only one theme's logo is set, the other silently reuses it
    (better than showing no logo at all in that theme)."""
    light = brand.get("logo_light_bytes")
    light = _img_data_uri(light) if light else (brand.get("logo_light_url", "").strip() or None)
    dark = brand.get("logo_dark_bytes")
    dark = _img_data_uri(dark) if dark else (brand.get("logo_dark_url", "").strip() or None)
    if light and not dark:
        dark = light
    if dark and not light:
        light = dark
    return light, dark


def _detected_theme_type():
    """'light' / 'dark' if this Streamlit version exposes the app's current
    theme server-side (st.context.theme, added in newer Streamlit - reflects
    the viewer's ACTUAL current theme, including a manual in-app override).
    None if unavailable - caller falls back to a pure-CSS (prefers-color-scheme)
    swap, which still gets it right for anyone on the default 'Auto' theme."""
    try:
        t = st.context.theme.type
        if t in ("light", "dark"):
            return t
    except Exception:
        pass
    return None


def has_brand_logo(brand: dict) -> bool:
    light, dark = _brand_logo_srcs(brand)
    return bool(light or dark)


def _render_brand_logo(brand: dict, width: int = None, centered: bool = False):
    """Renders the logo, automatically swapping between the light-theme and
    dark-theme image so it looks right either way, with an optional pulsing
    blue glow. Never raises - a bad/expired URL or corrupt upload should
    never take down the login page or sidebar, just show no logo.

    IMPORTANT: every line handed to st.markdown() here is built with ZERO
    leading whitespace. Markdown treats any line indented 4+ spaces as a
    preformatted code block - a natural-looking indented triple-quoted
    f-string (matching the surrounding Python code's indentation) renders
    as literal visible text instead of an actual <img>/<style>, which is
    exactly the bug this replaced (raw `<div>...` tags showing up on the
    login page instead of the logo)."""
    light_src, dark_src = _brand_logo_srcs(brand)
    if not light_src and not dark_src:
        return
    w = width or brand.get("logo_width", 220)
    justify = "center" if centered else "flex-start"
    glow = brand.get("logo_glow", True)
    glow_css = ("filter:drop-shadow(0 0 3px rgba(70,150,255,.4));"
                "animation:raiLogoGlow 2.4s ease-in-out infinite;") if glow else ""
    keyframes = ("@keyframes raiLogoGlow {"
                 "0%{filter:drop-shadow(0 0 2px rgba(70,150,255,.35)) drop-shadow(0 0 5px rgba(70,150,255,.15));}"
                 "50%{filter:drop-shadow(0 0 8px rgba(70,150,255,.9)) drop-shadow(0 0 18px rgba(70,150,255,.55));}"
                 "100%{filter:drop-shadow(0 0 2px rgba(70,150,255,.35)) drop-shadow(0 0 5px rgba(70,150,255,.15));}"
                 "}") if glow else ""
    try:
        theme = _detected_theme_type()
        if theme == "dark":
            src = dark_src
        elif theme == "light":
            src = light_src
        else:
            src = None  # unknown server-side - use the CSS media-query fallback below

        if src:
            html = (f'<style>{keyframes}</style>'
                     f'<div style="display:flex;justify-content:{justify};margin-bottom:0.3rem;">'
                     f'<img src="{src}" style="width:{w}px;height:auto;{glow_css}" />'
                     f'</div>')
        else:
            html = (f'<style>{keyframes}'
                     f'.rai-logo-l,.rai-logo-d{{width:{w}px;height:auto;{glow_css}}}'
                     f'.rai-logo-d{{display:none;}}'
                     f'@media (prefers-color-scheme: dark) {{.rai-logo-l{{display:none;}} .rai-logo-d{{display:inline-block;}}}}'
                     f'</style>'
                     f'<div style="display:flex;justify-content:{justify};margin-bottom:0.3rem;">'
                     f'<img class="rai-logo-l" src="{light_src}" />'
                     f'<img class="rai-logo-d" src="{dark_src}" />'
                     f'</div>')
        st.markdown(html, unsafe_allow_html=True)
    except Exception:
        pass


# ==================================================================================
# SESSION STATE INIT
# ==================================================================================
def init_state():
    ss = st.session_state
    ss.setdefault("authenticated", False)
    ss.setdefault("username", None)
    ss.setdefault("role", None)
    ss.setdefault("workspace_id", None)      # which data workspace this account owns (Phase 4: multi-tenant)
    ss.setdefault("view_as_workspace", None)  # admin-only: workspace_id currently being viewed/managed instead of their own
    ss.setdefault("_loaded_workspace_id", None)  # which workspace's data is currently sitting in session_state
    ss.setdefault("df_raw", None)
    ss.setdefault("meta", None)
    ss.setdefault("filters", {})
    ss.setdefault("theme", copy.deepcopy(DEFAULT_THEME))
    ss.setdefault("dashboard_charts", [])   # list of dicts: {family, variant} chosen for Boss Dashboard
    ss.setdefault("pinned_kpis", [])        # list of kpi labels pinned to dashboard
    ss.setdefault("p1_kpi_filters", {})     # {kpi_label: [filter,...]} — per-card filters, Raw Analysis KPI cards
    ss.setdefault("p1_kpi_number_format", "auto")  # global number format toolbar, Raw Analysis KPI cards
    ss.setdefault("page", "Connect Data")
    ss.setdefault("data_source_name", None)
    ss.setdefault("custom_kpis", [])        # list of user-built KPI card dicts (Custom Builder)
    ss.setdefault("custom_charts", [])      # list of user-built chart dicts (Custom Builder, Power-BI style)
    ss.setdefault("dashboard_slicers", [])  # list of dicts: {field, style} - Boss Dashboard slicer widgets
    ss.setdefault("dashboard_name", "⭐ Boss Dashboard")  # fully editable Boss Dashboard title
    # App branding (sidebar title) - GLOBAL across every account, admin-editable.
    # Loaded from disk once per session (not per-workspace - see workspace_store.load_branding).
    if "app_brand" not in ss:
        saved_brand = ws.load_branding()
        ss["app_brand"] = {**DEFAULT_BRAND, **saved_brand} if saved_brand else copy.deepcopy(DEFAULT_BRAND)
    # External Database Connector (Data Table page) - NEVER persisted to disk (see workspace_store.py)
    ss.setdefault("ai_chat_history", [])     # list of {role, content} — the AI Assistant page's chat log
    ss.setdefault("ai_groq_key", None)       # session-only OpenRouter API key typed into the UI by admin (never written to disk)
    ss.setdefault("db_conn_uri", "")
    ss.setdefault("db_conn_type", "PostgreSQL")
    ss.setdefault("db_connected", False)
    ss.setdefault("db_queries", [])         # list of query-tab dicts, see modules/db_connector.py
    ss.setdefault("db_query_results", {})   # {query_id: DataFrame}


init_state()


def effective_workspace_id():
    """The workspace whose data should be shown/edited on THIS run: an
    admin's own workspace_id, unless they've picked a client/viewer to
    'view as' in the sidebar, in which case that account's workspace_id
    takes over for the rest of this run."""
    ss = st.session_state
    if ss.role == auth.ROLE_ADMIN and ss.get("view_as_workspace"):
        return ss["view_as_workspace"]
    return ss.get("workspace_id") or ss.get("username")


def can_edit() -> bool:
    """True for admin and client accounts (can upload data, build/curate
    dashboards). False for viewer accounts (read-only - they can look at
    whatever their linked client/own workspace has, but never change it)."""
    return st.session_state.role in (auth.ROLE_ADMIN, auth.ROLE_CLIENT)


def can_edit_dashboard() -> bool:
    """Like can_edit(), but ALSO true for a 'report_viewer' (a restricted
    account a client can self-serve create for their own boss/manager —
    see Settings → My Report Viewers). A report_viewer only ever sees the
    Boss Dashboard page at all (everything else is hidden from their
    sidebar), but gets FULL control of what they do see there: pin/unpin
    cards, manage slicers, tweak the theme, export PDF — same as a client,
    just scoped to that one page."""
    return st.session_state.role in (auth.ROLE_ADMIN, auth.ROLE_CLIENT, auth.ROLE_REPORT_VIEWER)


def sync_workspace_from_disk(force: bool = False):
    """Call once near the top of every run, AFTER the effective workspace_id
    for this run is known. If it's different from what's currently sitting
    in session_state (first login, or an admin just switched 'view as'),
    load that workspace's saved data from disk - never invents empty state
    for a workspace, and never bleeds one workspace's data into another.

    force=True re-pulls the latest saved copy even when the workspace_id
    hasn't changed. Used on the Boss Dashboard page so that a client and
    their linked report-viewer(s) - who are two separate browser sessions
    sharing the SAME workspace - each see the other's changes (pinned/
    swapped charts, slicer picks, theme tweaks...) the moment they next
    interact with that page, instead of only after logging in fresh.
    Safe to do: auto-save (bottom of this file) writes the full current
    state to disk after EVERY single interaction in EVERY session sharing
    this workspace, so what's on disk is always at most a few hundred ms
    behind the most recent change from anyone."""
    ss = st.session_state
    wsid = effective_workspace_id()
    workspace_changed = ss.get("_loaded_workspace_id") != wsid
    if not workspace_changed and not force:
        return
    if workspace_changed:
        for k in ws.PERSISTED_KEYS:
            ss[k] = [] if isinstance(ss.get(k), list) else None
        ss["filters"] = {}
        ss["p3_sql_result"] = None  # SQL Query tab result belongs to the previous workspace — drop it on switch
        ss["p3_sql_error"] = None
    else:
        # Same workspace, just refreshing (force=True path). Several buttons on
        # this page (Remove/pin/unpin/etc.) call st.rerun() immediately after
        # changing session_state, which SKIPS the normal end-of-script
        # auto-save for that run entirely (it lives at the very bottom of
        # this file, which an immediate rerun never reaches). Without this,
        # the very next run's reload below would load a stale pre-change
        # copy from disk and silently undo whatever was just clicked - that
        # was making Remove/pin/etc. look broken.
        # Fix: flush THIS session's current state to disk FIRST, then read
        # back. If this session made the latest change, that's a no-op (we
        # just read back what we wrote). If a DIFFERENT session (e.g. a
        # linked report-viewer, in another browser) saved something even
        # more recently, we correctly pick up THEIR newer version instead.
        if ss.get("df_raw") is not None:
            ws.save(ss, wsid)
    saved = ws.load(wsid)
    if saved:
        for k, v in saved.items():
            if v is not None:
                ss[k] = v
    # dashboard_name is a plain string with a non-None default (set in init_state).
    # Old workspace saves made before this field existed (or a save with the
    # title cleared) can leave it as None here, which crashes the Boss
    # Dashboard title editor (`new_name.strip()` on None). Always fall back
    # to the default instead of leaving it None.
    if not ss.get("dashboard_name"):
        ss["dashboard_name"] = "⭐ Boss Dashboard"
    ss["_loaded_workspace_id"] = wsid


# ==================================================================================
# AUTH GATE
# ==================================================================================
# The admin login form is NEVER shown as a button/icon on the normal sign-in
# screen - a regular report-user should have no visual hint that it exists at
# all. It only appears if the page is opened with this exact secret value in
# the URL, e.g.  http://localhost:8501/?admin=SET-YOUR-OWN-SECRET-HERE
# Change this string to whatever private value you want, then bookmark the URL
# with it for yourself. Anyone without that exact link just sees a plain login.
ADMIN_URL_KEY = "admin"
ADMIN_URL_SECRET = "SET-YOUR-OWN-SECRET-HERE"

# --------------------------------------------------------------------------------
# "STAY LOGGED IN" TOKEN STORAGE: a real browser cookie, not a URL query param.
# --------------------------------------------------------------------------------
# The token used to be put in the URL as ?s=<token>. That "worked" for
# surviving a refresh, but it meant the LOGIN ITSELF was embedded in the
# address bar text — copy that URL and paste it into a different browser,
# a different device, or send it to someone else, and THEY were instantly
# logged in as you, no password asked. That's exactly the leak that was
# reported.
#
# A real cookie fixes this: it's still remembered by THIS browser across a
# refresh/reopen (via st.context.cookies, which reflects whatever cookies
# the browser actually sent with the page request), but it is never part of
# the URL text, so copy-pasting the link into another browser/device carries
# no credential at all — that browser has to log in for real. Opening a
# second TAB in the *same* browser will still be logged in, same as any
# normal website (Gmail, etc.) — that's expected, not a leak, since it's
# still the same physical browser holding the cookie.
SESSION_COOKIE_NAME = "app_session"


def _set_session_cookie(token: str):
    """Sets the 'stay logged in' cookie, then reruns - but only AFTER giving
    the browser a moment to actually execute the cookie-setting script below.

    Why the delay: components.html() renders an iframe; the browser has to
    receive it and run its <script> before the cookie exists. Calling
    st.rerun() immediately (no delay) used to race that - Streamlit could
    tear the page down before the script ran, so the cookie was silently
    never set (refresh -> bounced back to login).

    An earlier version of this fix tried to reload via
    `window.parent.location.reload()` from inside that same script instead
    of a delay. That's WRONG: Streamlit's component iframe is sandboxed, and
    a sandboxed iframe cannot navigate its parent's top-level page unless
    the iframe is explicitly given that permission - browsers silently block
    it. That's why login stopped working entirely after that attempt: the
    cookie script ran, but the page reload it tried to trigger never
    happened, so the app just sat there. A short server-side sleep before a
    normal st.rerun() sidesteps that restriction completely - no navigation
    is attempted from inside the iframe at all."""
    import streamlit.components.v1 as components
    components.html(
        f"""<script>
        document.cookie = "{SESSION_COOKIE_NAME}={token}; path=/; max-age={auth.SESSION_LIFETIME_SECONDS}; SameSite=Lax";
        </script>""",
        height=0, width=0,
    )
    time.sleep(0.2)
    st.rerun()


def _clear_session_cookie():
    """Same reasoning as _set_session_cookie() above - a short delay before
    rerunning, no iframe-triggered navigation."""
    import streamlit.components.v1 as components
    components.html(
        f"""<script>
        document.cookie = "{SESSION_COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax";
        </script>""",
        height=0, width=0,
    )
    time.sleep(0.2)
    st.rerun()


def _get_session_cookie():
    try:
        return st.context.cookies.get(SESSION_COOKIE_NAME)
    except Exception:
        return None  # older Streamlit without st.context.cookies — fails safe (just asks to log in again)


def login_screen():
    _b = st.session_state.app_brand
    _render_brand_logo(_b, centered=True)
    # A configured logo already carries the product name/wordmark - showing the
    # generic "Sports Analytics Platform" text heading underneath it as well
    # would be redundant. Only fall back to that text title when no logo is set.
    if not (_b.get("hide_login_title", True) and has_brand_logo(_b)):
        st.markdown("<h1 style='text-align:center;'> 🆁🅰-🅸 </h1>", unsafe_allow_html=True)
        st.markdown("<p style='text-align:center;font-size:24px;color:#666;'>🆁🅴🆂🅴🅰🆁🅲🅷 🅰🅽🅰🅻🆈🆃🅸🅲🆂 🅸🅽🆃🅴🅻🅻🅸🅶🅴🅽🅲🅴</p>", unsafe_allow_html=True)
    st.markdown("<p style='text-align:center;color:gray;'>Please sign in to continue</p>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        with st.form("login_form"):
            u = st.text_input("Username")
            p = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)
            if submitted:
                role = auth.verify_login(u, p)
                if role:
                    st.session_state.authenticated = True
                    st.session_state.username = u.strip()
                    st.session_state.role = role
                    st.session_state.workspace_id = auth.get_workspace_id(u.strip())
                    _token = auth.create_session(u.strip())
                    st.session_state._session_token = _token
                    _set_session_cookie(_token)  # survives a browser refresh, but not copy-paste into another browser
                else:
                    st.error("Invalid username or password.")

        with st.expander("Forgot password?"):
            with st.form("forgot_pw_form"):
                fp_email = st.text_input("Your account's email address")
                fp_submit = st.form_submit_button("Send reset link")
                if fp_submit:
                    uname = auth.find_username_by_email(fp_email)
                    if uname:
                        token = auth.create_password_reset_token(uname)
                        try:
                            base_url = st.secrets.get("APP_BASE_URL", "")
                        except Exception:
                            base_url = ""
                        if not base_url:
                            base_url = "https://reportgenai-2uk4jqmjulsachx5vgg6wz.streamlit.app/"  # fallback if APP_BASE_URL secret isn't set
                        reset_url = f"{base_url.rstrip('/')}/?reset={token}"
                        ok, msg = es.send_password_reset_email(fp_email.strip(), uname, reset_url)
                        if not ok:
                            st.error(msg)   # a real send failure (e.g. not configured) - safe to show, no account info leaked
                    # Same message whether or not the email matched an account, on purpose -
                    # otherwise this box could be used to check who has an account here.
                    st.success("If that email is on an account here, a reset link has been sent "
                               "(check spam too). It expires in 30 minutes.")

        admin_link_used = st.query_params.get(ADMIN_URL_KEY) == ADMIN_URL_SECRET
        if admin_link_used:
            st.write("")
            st.info("Admin login — for the person who manages this tool's users, not for daily reporting.")
            with st.form("admin_login_form"):
                au = st.text_input("Admin username", key="admin_u")
                ap = st.text_input("Admin password", type="password", key="admin_p")
                admin_submitted = st.form_submit_button("Admin Login", use_container_width=True)
                if admin_submitted:
                    if auth.verify_admin_login(au, ap):
                        st.session_state.authenticated = True
                        st.session_state.username = au.strip()
                        st.session_state.role = auth.ROLE_ADMIN
                        st.session_state.workspace_id = auth.get_workspace_id(au.strip())
                        _token = auth.create_session(au.strip())
                        st.session_state._session_token = _token
                        _set_session_cookie(_token)
                    else:
                        st.error("Invalid admin username or password.")


def reset_password_screen(token: str):
    """Shown instead of the normal login screen when the URL has ?reset=<token>
    (i.e. the person clicked the link from their "forgot password" email)."""
    st.markdown("<h1 style='text-align:center;'>🔑 Set a new password</h1>", unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        uname = auth.resolve_password_reset_token(token)
        if not uname:
            st.error("This reset link is invalid or has expired (links only work for 30 minutes "
                      "and only once). Go back and request a new one.")
            if st.button("← Back to login"):
                del st.query_params["reset"]
                st.rerun()
            return
        st.caption(f"Setting a new password for **{uname}**.")
        with st.form("forgot_reset_pw_form"):
            np1 = st.text_input("New password", type="password")
            np2 = st.text_input("Confirm new password", type="password")
            submit = st.form_submit_button("Set new password", use_container_width=True)
            if submit:
                if not np1 or np1 != np2:
                    st.error("Passwords are empty or don't match.")
                else:
                    auth.change_password(uname, np1)
                    auth.consume_password_reset_token(token)   # one-time use — dead now regardless of outcome
                    del st.query_params["reset"]
                    st.success("Password updated! You can log in with it now.")
                    st.button("← Go to login")  # just a visual nudge; the query param is already cleared


_reset_token = st.query_params.get("reset")
if _reset_token and not st.session_state.authenticated:
    reset_password_screen(_reset_token)
    st.stop()

# A genuine browser refresh wipes st.session_state (a brand-new Streamlit session
# starts), which used to bounce people straight back to the login screen just for
# hitting F5/reload. Before falling back to the login screen, check the ?s=...
# token Streamlit kept in the URL across that reload — if it's still valid, log
# the person back in silently instead of making them type their password again.
if not st.session_state.authenticated:
    _session_token = _get_session_cookie()
    _resolved_user = auth.resolve_session(_session_token) if _session_token else None
    if _resolved_user and auth.user_exists(_resolved_user):
        st.session_state.authenticated = True
        st.session_state.username = _resolved_user
        st.session_state.role = auth.get_role(_resolved_user)
        st.session_state.workspace_id = auth.get_workspace_id(_resolved_user)
        st.session_state._session_token = _session_token

if not st.session_state.authenticated:
    login_screen()
    st.stop()


# ==================================================================================
# HELPERS
# ==================================================================================
def get_style_dict():
    th = st.session_state.theme
    return {
        "palette": PALETTES.get(th["palette_name"], PALETTES["Set2"]),
        "template": th["template"],
        "font_family": th["font_family"],
        "font_color": th["font_color"],
        "font_size": th["font_size"],
        "show_legend": th["show_legend"],
        "show_labels": th["show_labels"],
        "chart_bg": th["chart_bg"],
        "plot_bg": th["plot_bg"],
    }


def _read_upload(uploaded_file):
    """Safely reads bytes out of a Streamlit UploadedFile.

    UploadedFile behaves like a one-shot stream: the FIRST time something
    calls .read() on it you get the real bytes, but on every later Streamlit
    rerun (every click anywhere on the page re-runs this whole script) the
    SAME object is handed back already at end-of-stream, so a second
    .read() silently returns b"" — which then made pandas/Excel parsing
    blow up with a confusing ValueError, and separately made the PDF
    wallpaper "disappear" after navigating away and back. seek(0) first,
    every time, fixes both.
    """
    uploaded_file.seek(0)
    return uploaded_file.read()


@st.cache_data(show_spinner=False)
def _load_and_clean(file_bytes, file_name):
    class _F:
        def __init__(self, b, n):
            self._b = b
            self.name = n
        def read(self):
            return self._b
    sheets = de.load_dataframe(_F(file_bytes, file_name))
    cleaned = {name: de.clean_dataframe(df) for name, df in sheets.items()}
    return cleaned


def load_file(uploaded_file):
    """Single-file load (kept for backward compatibility / the sample loader)."""
    file_bytes = _read_upload(uploaded_file)
    sheets = _load_and_clean(file_bytes, uploaded_file.name)
    if len(sheets) > 1:
        sheet_name = st.selectbox("Multiple sheets found — pick one", list(sheets.keys()), key="sheet_pick")
    else:
        sheet_name = list(sheets.keys())[0]
    df = sheets[sheet_name]
    if df is None or df.empty:
        st.error("Could not read any usable rows from this file.")
        return None
    _apply_loaded_df(df, uploaded_file.name)
    return df


def _apply_loaded_df(df, source_name):
    st.session_state.df_raw = df
    st.session_state.meta = de.profile_columns(df)
    st.session_state.data_source_name = source_name
    st.session_state.filters = {}
    st.session_state.dashboard_charts = []
    st.session_state.pinned_kpis = []
    st.session_state.dashboard_slicers = []


def load_files(uploaded_files, combine_mode="stack"):
    """Loads MULTIPLE uploaded files and combines them into a single dataset.
    combine_mode: 'stack' (append rows, union columns, tag each row with its
    source file) or 'columns' (paste sheets side by side)."""
    if not uploaded_files:
        return None

    if len(uploaded_files) == 1:
        return load_file(uploaded_files[0])

    named_dfs = []
    for f in uploaded_files:
        file_bytes = _read_upload(f)
        sheets = _load_and_clean(file_bytes, f.name)
        # if a file itself has multiple sheets, stack/union all of them under that file's name
        for sheet_name, sdf in sheets.items():
            if sdf is None or sdf.empty:
                continue
            label = f.name if len(sheets) == 1 else f"{f.name} [{sheet_name}]"
            named_dfs.append((label, sdf))

    if not named_dfs:
        st.error("Could not read any usable rows from these files.")
        return None

    combined = de.combine_dataframes(named_dfs, mode=combine_mode)
    if combined is None or combined.empty:
        st.error("Combining these files produced no usable rows — check they share a similar structure.")
        return None

    combined_name = f"{len(uploaded_files)} files combined ({', '.join(f.name for f in uploaded_files[:3])}{', ...' if len(uploaded_files) > 3 else ''})"
    _apply_loaded_df(combined, combined_name)
    return combined


def load_sample():
    if not os.path.exists(SAMPLE_PATH):
        st.error("Sample file not found.")
        return
    with open(SAMPLE_PATH, "rb") as f:
        data = _read_upload(f)
    sheets = _load_and_clean(data, "sample_sports_payments.csv")
    df = sheets["Sheet1"]
    st.session_state.df_raw = df
    st.session_state.meta = de.profile_columns(df)
    st.session_state.data_source_name = "sample_sports_payments.csv (demo data)"
    st.session_state.filters = {}
    st.session_state.dashboard_charts = []
    st.session_state.pinned_kpis = []
    st.session_state.dashboard_slicers = []


def render_filters(df, meta, key_prefix=""):
    """Draws filter widgets for every column and returns the filtered dataframe."""
    filters = st.session_state.filters
    # NOTE: a column can appear in BOTH categorical_cols and status_cols (data_engine
    # tags "status/stage/state" columns into status_cols without excluding them from
    # categorical_cols). De-dupe here so we never build two widgets with the same key.
    cat_and_status_cols = list(dict.fromkeys(meta["categorical_cols"] + meta["status_cols"]))
    with st.expander("🔎 Filters (apply to every KPI & chart below)", expanded=False):
        cols_ui = st.columns(3)
        i = 0
        for col in cat_and_status_cols:
            if col not in df.columns:
                continue
            with cols_ui[i % 3]:
                opts = sorted([str(x) for x in df[col].dropna().unique()])[:500]
                sel = st.multiselect(col, opts, default=filters.get(f"{key_prefix}{col}", []), key=f"{key_prefix}filt_{col}")
                filters[f"{key_prefix}{col}"] = sel
            i += 1
        for col in meta["numeric_cols"]:
            if col not in df.columns:
                continue
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if s.empty:
                continue
            lo, hi = float(s.min()), float(s.max())
            if lo == hi:
                continue
            with cols_ui[i % 3]:
                default = filters.get(f"{key_prefix}{col}_range", (lo, hi))
                rng = st.slider(col, lo, hi, default, key=f"{key_prefix}filt_num_{col}")
                filters[f"{key_prefix}{col}_range"] = rng
            i += 1
        for col in meta["date_cols"]:
            if col not in df.columns:
                continue
            d = pd.to_datetime(df[col], errors="coerce").dropna()
            if d.empty:
                continue
            lo, hi = d.min().date(), d.max().date()
            with cols_ui[i % 3]:
                default = filters.get(f"{key_prefix}{col}_daterange", (lo, hi))
                rng = st.date_input(col, default, key=f"{key_prefix}filt_date_{col}")
                filters[f"{key_prefix}{col}_daterange"] = rng
            i += 1
        c1, c2 = st.columns([1, 5])
        with c1:
            if st.button("Reset filters", key=f"{key_prefix}reset_filters"):
                st.session_state.filters = {}
                st.rerun()

    fdf = df.copy()
    for col in cat_and_status_cols:
        sel = filters.get(f"{key_prefix}{col}")
        if sel:
            fdf = fdf[fdf[col].astype(str).isin(sel)]
    for col in meta["numeric_cols"]:
        rng = filters.get(f"{key_prefix}{col}_range")
        if rng and col in fdf.columns:
            s = pd.to_numeric(fdf[col], errors="coerce")
            fdf = fdf[(s >= rng[0]) & (s <= rng[1]) | s.isna()]
    for col in meta["date_cols"]:
        rng = filters.get(f"{key_prefix}{col}_daterange")
        if rng and isinstance(rng, (tuple, list)) and len(rng) == 2 and col in fdf.columns:
            d = pd.to_datetime(fdf[col], errors="coerce")
            fdf = fdf[(d.dt.date >= rng[0]) & (d.dt.date <= rng[1]) | d.isna()]
    return fdf


SLICER_STYLES = ["Dropdown", "Vertical list", "Tile"]


def render_slicers(df, meta, key_prefix="", editable=None):
    """Power-BI style slicers: unlike the generic 'Filters' expander above
    (which always lists every column, tucked away collapsed), a slicer is a
    report element YOU explicitly add for one chosen field, in a chosen visual
    style, and it stays visible on the dashboard itself. Returns the further-
    filtered dataframe (applied on top of whatever render_filters() already
    narrowed down).
    `editable` lets the caller override the default can_edit() gate — Boss
    Dashboard passes can_edit_dashboard() so a report_viewer (who can only
    ever reach that one page) gets full slicer control there too."""
    if editable is None:
        editable = can_edit()
    slicers = st.session_state.dashboard_slicers
    all_cols = list(df.columns)

    with st.expander("🎚️ Manage Slicers (pick which fields show as filter widgets on this dashboard)", expanded=False):
        if not editable:
            st.caption("View-only account — slicer setup is managed by your admin/client.")
        else:
            used_fields = [s["field"] for s in slicers]
            available = [c for c in all_cols if c not in used_fields]
            if available:
                ac1, ac2, ac3 = st.columns([3, 2, 1])
                with ac1:
                    new_field = st.selectbox("Field", available, key=f"{key_prefix}slicer_new_field")
                with ac2:
                    new_style = st.selectbox("Style", SLICER_STYLES, key=f"{key_prefix}slicer_new_style")
                with ac3:
                    st.write("")
                    st.write("")
                    if st.button("➕ Add slicer", key=f"{key_prefix}slicer_add", use_container_width=True):
                        slicers.append({"field": new_field, "style": new_style})
                        st.rerun()
            else:
                st.caption("Every column already has a slicer.")

            if slicers:
                st.divider()
                st.caption("Slicers on this dashboard:")
                for i, s in enumerate(list(slicers)):
                    r1, r2, r3 = st.columns([3, 2, 1])
                    with r1:
                        st.write(f"**{s['field']}**")
                    with r2:
                        if s["field"] in (meta.get("categorical_cols", []) + meta.get("status_cols", [])):
                            s["style"] = st.selectbox("Style", SLICER_STYLES,
                                                       index=SLICER_STYLES.index(s["style"]) if s["style"] in SLICER_STYLES else 0,
                                                       key=f"{key_prefix}slicer_style_{i}", label_visibility="collapsed")
                        else:
                            st.caption("range widget")
                    with r3:
                        if st.button("🗑️", key=f"{key_prefix}slicer_rm_{i}", help="Remove this slicer"):
                            slicers.pop(i)
                            st.rerun()

    if not slicers:
        return df

    st.markdown("##### 🎚️ Slicers")
    view = df.copy()
    n = len(slicers)
    cols = st.columns(min(n, 4))
    for i, s in enumerate(slicers):
        field, style = s["field"], s["style"]
        if field not in df.columns:
            continue
        with cols[i % len(cols)]:
            st.caption(field)
            skey = f"{key_prefix}slicer_val_{field}"
            if field in meta.get("numeric_cols", []):
                series = pd.to_numeric(df[field], errors="coerce").dropna()
                if series.empty:
                    continue
                lo, hi = float(series.min()), float(series.max())
                if lo == hi:
                    continue
                rng = st.slider(field, lo, hi, (lo, hi), key=skey, label_visibility="collapsed")
                s_num = pd.to_numeric(view[field], errors="coerce")
                view = view[(s_num >= rng[0]) & (s_num <= rng[1])]
            elif field in meta.get("date_cols", []):
                d = pd.to_datetime(df[field], errors="coerce").dropna()
                if d.empty:
                    continue
                lo, hi = d.min().date(), d.max().date()
                rng = st.date_input(field, (lo, hi), key=skey, label_visibility="collapsed")
                if isinstance(rng, (tuple, list)) and len(rng) == 2:
                    d_col = pd.to_datetime(view[field], errors="coerce")
                    view = view[(d_col.dt.date >= rng[0]) & (d_col.dt.date <= rng[1])]
            else:
                opts = sorted([str(x) for x in df[field].dropna().unique()])[:200]
                if not opts:
                    continue
                sel = []
                if style == "Dropdown":
                    sel = st.multiselect(field, opts, key=skey, label_visibility="collapsed")
                elif style == "Vertical list":
                    with st.container(border=True):
                        for opt in opts:
                            if st.checkbox(opt, key=f"{skey}_{opt}"):
                                sel.append(opt)
                else:  # Tile
                    with st.container(border=True):
                        tile_cols = st.columns(3)
                        for oi, opt in enumerate(opts):
                            with tile_cols[oi % 3]:
                                if st.checkbox(opt, key=f"{skey}_{opt}"):
                                    sel.append(opt)
                if sel:
                    view = view[view[field].astype(str).isin(sel)]
    return view


def kpi_cards(kpis, pinnable=False, key_prefix="", df=None, filterable=False, number_format="auto", removable=False):
    """Renders KPI cards in a responsive grid. If pinnable, shows a pin checkbox per card.
    If filterable (needs `df`), every card that represents a plain column aggregation
    (has "column"/"agg" set — see de.compute_kpis) gets its OWN "➕ Filter this card"
    popover, independent of every other card and independent of the page-level filter.
    If removable, shows a 🗑️ button that unpins the card right here (e.g. on the Boss
    Dashboard) — same effect as un-ticking its ⭐ back on Raw Analysis, but without
    having to leave the page to do it."""
    n_cols = 4
    store = st.session_state.p1_kpi_filters
    for row_start in range(0, len(kpis), n_cols):
        cols = st.columns(n_cols)
        for j, k in enumerate(kpis[row_start:row_start + n_cols]):
            with cols[j]:
                label = k["label"]
                value, sub = k["value"], k.get("sub")
                if filterable and df is not None and k.get("column") and k.get("agg"):
                    card_filters = store.get(label, [])
                    if card_filters:
                        fdf = ms.apply_filters(df, card_filters)
                        col, agg = k["column"], k["agg"]
                        s = pd.to_numeric(fdf[col], errors="coerce") if agg in ("sum", "mean") else fdf[col]
                        if agg == "sum":
                            value = de._fmt_num(s.sum(), number_format)
                        elif agg == "mean":
                            value = de._fmt_num(s.mean(), number_format)
                        elif agg == "nunique":
                            value = f"{s.nunique():,}"
                        sub = f"{sub} · {len(fdf):,} rows after this card's filter"
                    st.metric(label, value, help=sub)
                    with st.popover("➕ Filter this card", use_container_width=True):
                        new_filters = be.render_filter_builder(df, card_filters, key_prefix=f"{key_prefix}kpi_{label}_")
                        store[label] = new_filters
                else:
                    st.metric(label, value, help=sub)
                if pinnable:
                    pinned = label in st.session_state.pinned_kpis
                    new_val = st.checkbox("⭐ pin to dashboard", value=pinned, key=f"{key_prefix}pin_{label}_{row_start}_{j}")
                    if new_val and label not in st.session_state.pinned_kpis:
                        st.session_state.pinned_kpis.append(label)
                    if not new_val and label in st.session_state.pinned_kpis:
                        st.session_state.pinned_kpis.remove(label)
                if removable:
                    if st.button("🗑️ Remove", key=f"{key_prefix}rm_{label}_{row_start}_{j}",
                                  help="Unpin this card from the dashboard", use_container_width=True):
                        if label in st.session_state.pinned_kpis:
                            st.session_state.pinned_kpis.remove(label)
                        st.rerun()


def chart_in_dashboard(family, variant_id):
    return any(c["family"] == family and c["variant"]["id"] == variant_id for c in st.session_state.dashboard_charts)


def add_to_dashboard(family, variant):
    # Only replaces an EXACT same (family, variant id) match - so pinning a second,
    # different Bar variant no longer knocks the first Bar chart off the dashboard.
    # As many variants per family as you like can be pinned side by side.
    vid = variant["id"]
    st.session_state.dashboard_charts = [
        c for c in st.session_state.dashboard_charts
        if not (c["family"] == family and c["variant"]["id"] == vid)
    ]
    st.session_state.dashboard_charts.append({"family": family, "variant": copy.deepcopy(variant)})


def remove_from_dashboard(family, variant_id):
    st.session_state.dashboard_charts = [
        c for c in st.session_state.dashboard_charts
        if not (c["family"] == family and c["variant"]["id"] == variant_id)
    ]


def _safe_index(options, value, fallback=0):
    try:
        return options.index(value)
    except (ValueError, TypeError):
        return fallback if options else 0


def customize_variant(fam, variant, meta, key_prefix):
    """Draws an '⚙️ Customize' expander under a chart with the right X / Y /
    measure / aggregation / split controls for that chart family, built from
    whatever columns exist in the current dataset (nothing hard-coded).
    Returns a (possibly edited) COPY of the variant - if anything was changed,
    it gets its own unique id so pinning it to the Boss Dashboard never clashes
    with the original auto-generated version of this chart."""
    cats = meta["categorical_cols"] or []
    nums = meta["numeric_cols"] or []
    dates = meta["date_cols"] or []
    v = copy.deepcopy(variant)
    changed = False
    NONE_LABEL = "(none)"
    COUNT_LABEL = "(count of rows)"

    with st.expander("⚙️ Customize X / Y / measure / filter"):
        if fam == "Bar":
            dim = st.selectbox("Category (X)", cats, index=_safe_index(cats, v.get("dim")), key=f"{key_prefix}dim") if cats else v.get("dim")
            m_opts = [COUNT_LABEL] + nums
            cur = v.get("measure") or COUNT_LABEL
            m = st.selectbox("Measure (Y)", m_opts, index=_safe_index(m_opts, cur), key=f"{key_prefix}measure")
            agg = st.selectbox("Aggregation", ["sum", "mean", "median", "max", "min"],
                                index=_safe_index(["sum", "mean", "median", "max", "min"], v.get("agg", "sum")),
                                key=f"{key_prefix}agg")
            m = None if m == COUNT_LABEL else m
            if (dim, m, agg) != (v.get("dim"), v.get("measure"), v.get("agg")):
                v["dim"], v["measure"], v["agg"] = dim, m, agg
                v["title"] = f"{agg.title()} {m or 'Records'} by {dim}"
                changed = True

        elif fam in ("Line", "Area"):
            date_opts = [NONE_LABEL] + dates
            cur_d = v.get("date_col") or NONE_LABEL
            dcol = st.selectbox("Date (X)", date_opts, index=_safe_index(date_opts, cur_d), key=f"{key_prefix}date")
            dcol = None if dcol == NONE_LABEL else dcol
            m = st.selectbox("Measure (Y)", nums, index=_safe_index(nums, v.get("measure")), key=f"{key_prefix}measure") if nums else v.get("measure")
            gran = st.selectbox("Granularity", ["D", "W", "ME"], format_func=lambda x: {"D": "Daily", "W": "Weekly", "ME": "Monthly"}[x],
                                 index=_safe_index(["D", "W", "ME"], v.get("gran", "D")), key=f"{key_prefix}gran") if dcol else v.get("gran")
            split_opts = [NONE_LABEL] + cats
            cur_split = v.get("split_by") or NONE_LABEL
            split_by = st.selectbox("Split by (color)", split_opts, index=_safe_index(split_opts, cur_split), key=f"{key_prefix}split")
            split_by = None if split_by == NONE_LABEL else split_by
            new_vals = {"date_col": dcol, "measure": m, "gran": gran, "split_by": split_by}
            if fam == "Area":
                cum = st.checkbox("Cumulative", value=v.get("cum", False), key=f"{key_prefix}cum")
                new_vals["cum"] = cum
            if any(new_vals.get(k) != v.get(k) for k in new_vals):
                v.update(new_vals)
                v["title"] = f"{'Cumulative ' if v.get('cum') else ''}{m} over time" + (f" by {split_by}" if split_by else "")
                changed = True

        elif fam == "Pie":
            dim = st.selectbox("Category", cats, index=_safe_index(cats, v.get("dim")), key=f"{key_prefix}dim") if cats else v.get("dim")
            m_opts = [COUNT_LABEL] + nums
            cur = v.get("measure") or COUNT_LABEL
            m = st.selectbox("Measure", m_opts, index=_safe_index(m_opts, cur), key=f"{key_prefix}measure")
            m = None if m == COUNT_LABEL else m
            if (dim, m) != (v.get("dim"), v.get("measure")):
                v["dim"], v["measure"] = dim, m
                v["title"] = f"Share of {m or 'Records'} by {dim}"
                changed = True

        elif fam == "Comparison":
            dim1 = st.selectbox("Category 1", cats, index=_safe_index(cats, v.get("dim1")), key=f"{key_prefix}dim1") if cats else v.get("dim1")
            dim2_opts = [c for c in cats if c != dim1] or cats
            dim2 = st.selectbox("Category 2", dim2_opts, index=_safe_index(dim2_opts, v.get("dim2")), key=f"{key_prefix}dim2") if dim2_opts else v.get("dim2")
            m = st.selectbox("Measure", nums, index=_safe_index(nums, v.get("measure")), key=f"{key_prefix}measure") if nums else v.get("measure")
            if (dim1, dim2, m) != (v.get("dim1"), v.get("dim2"), v.get("measure")):
                v["dim1"], v["dim2"], v["measure"] = dim1, dim2, m
                v["title"] = f"{m or 'Records'}: {dim1} vs {dim2}"
                changed = True

        elif fam == "Scatter":
            x = st.selectbox("X axis", nums, index=_safe_index(nums, v.get("x")), key=f"{key_prefix}x") if nums else v.get("x")
            y_opts = [n for n in nums if n != x] or nums
            y = st.selectbox("Y axis", y_opts, index=_safe_index(y_opts, v.get("y")), key=f"{key_prefix}y") if y_opts else v.get("y")
            color_opts = [NONE_LABEL] + cats
            cur_c = v.get("color") or NONE_LABEL
            color = st.selectbox("Color by", color_opts, index=_safe_index(color_opts, cur_c), key=f"{key_prefix}color")
            color = None if color == NONE_LABEL else color
            if (x, y, color) != (v.get("x"), v.get("y"), v.get("color")):
                v["x"], v["y"], v["color"] = x, y, color
                v["title"] = f"{x} vs {y}" + (f" by {color}" if color else "")
                changed = True

        elif fam == "Box":
            dim = st.selectbox("Category (X)", cats, index=_safe_index(cats, v.get("dim")), key=f"{key_prefix}dim") if cats else v.get("dim")
            m = st.selectbox("Measure (Y)", nums, index=_safe_index(nums, v.get("measure")), key=f"{key_prefix}measure") if nums else v.get("measure")
            if (dim, m) != (v.get("dim"), v.get("measure")):
                v["dim"], v["measure"] = dim, m
                v["title"] = f"Distribution of {m} across {dim}"
                changed = True

        elif fam == "Histogram":
            m = st.selectbox("Measure", nums, index=_safe_index(nums, v.get("measure")), key=f"{key_prefix}measure") if nums else v.get("measure")
            bins = st.slider("Bins", 5, 100, v.get("bins", 30), key=f"{key_prefix}bins")
            facet_opts = [NONE_LABEL] + cats
            cur_f = v.get("facet") or NONE_LABEL
            facet = st.selectbox("Split by", facet_opts, index=_safe_index(facet_opts, cur_f), key=f"{key_prefix}facet")
            facet = None if facet == NONE_LABEL else facet
            if (m, bins, facet) != (v.get("measure"), v.get("bins"), v.get("facet")):
                v["measure"], v["bins"], v["facet"] = m, bins, facet
                v["title"] = f"Distribution of {m} ({bins} bins)" + (f" split by {facet}" if facet else "")
                changed = True

        elif fam == "Treemap":
            path = st.multiselect("Hierarchy (in order)", cats, default=v.get("path", cats[:1]), key=f"{key_prefix}path")
            m = st.selectbox("Measure", nums, index=_safe_index(nums, v.get("measure")), key=f"{key_prefix}measure") if nums else v.get("measure")
            if path and (path, m) != (v.get("path"), v.get("measure")):
                v["path"], v["measure"] = path, m
                v["title"] = f"{m or 'Records'} Treemap - " + " > ".join(path)
                changed = True

        elif fam == "Heatmap" and v.get("kind") == "cross":
            dim1 = st.selectbox("Rows", cats, index=_safe_index(cats, v.get("dim1")), key=f"{key_prefix}dim1") if cats else v.get("dim1")
            dim2_opts = [c for c in cats if c != dim1] or cats
            dim2 = st.selectbox("Columns", dim2_opts, index=_safe_index(dim2_opts, v.get("dim2")), key=f"{key_prefix}dim2") if dim2_opts else v.get("dim2")
            m_opts = [COUNT_LABEL] + nums
            cur = v.get("measure") or COUNT_LABEL
            m = st.selectbox("Measure", m_opts, index=_safe_index(m_opts, cur), key=f"{key_prefix}measure")
            m = None if m == COUNT_LABEL else m
            if (dim1, dim2, m) != (v.get("dim1"), v.get("dim2"), v.get("measure")):
                v["dim1"], v["dim2"], v["measure"] = dim1, dim2, m
                v["title"] = f"{m or 'Records'} Heatmap: {dim1} x {dim2}"
                changed = True

    if changed:
        sig = hashlib.md5(repr(sorted(v.items(), key=lambda kv: kv[0])).encode()).hexdigest()[:10]
        v["id"] = f"{fam}_custom_{sig}"
    return v


# ==================================================================================
# SIDEBAR NAV
# ==================================================================================
with st.sidebar:
    _b = st.session_state.app_brand
    _render_brand_logo(_b, width=min(_b.get("logo_width", 220), 160))
    st.markdown(
        f"<div style='font-size:{_b['font_size']}px; color:{_b['color']}; "
        f"font-weight:{'700' if _b['bold'] else '400'}; "
        f"font-style:{'italic' if _b['italic'] else 'normal'}; "
        f"font-family:{_b['font_family']}; margin-bottom:0.2rem;'>{_b['text']}</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Logged in as **{st.session_state.username}** ({st.session_state.role})")

    # ---- Admin-only: "View as" a client/viewer workspace ------------------------
    # Admin has no data of its own - it borrows whichever account's workspace is
    # picked here for the rest of this run (Raw Analysis, Boss Dashboard, Data
    # Table all follow this pick). Clears back to "(My own data)" on next login.
    if st.session_state.role == auth.ROLE_ADMIN:
        all_users = auth.list_users()
        # one entry per distinct workspace_id, labelled with everyone sharing it
        ws_to_accounts = {}
        for u in all_users:
            if u["role"] == auth.ROLE_ADMIN:
                continue
            ws_to_accounts.setdefault(u["workspace_id"], []).append(f"{u['username']} ({u['role']})")
        ws_labels = {wsid: f"{wsid}  —  {', '.join(names)}" for wsid, names in ws_to_accounts.items()}
        options = ["(My own data)"] + list(ws_labels.keys())
        current = st.session_state.get("view_as_workspace") or "(My own data)"
        chosen = st.selectbox(
            "👁️ View as", options, index=options.index(current) if current in options else 0,
            format_func=lambda x: ws_labels.get(x, x),
            help="Pick a client/viewer's workspace to view and manage their data. Nothing here affects any other client.",
        )
        st.session_state.view_as_workspace = None if chosen == "(My own data)" else chosen
        if st.session_state.view_as_workspace:
            st.info(f"👁️ Viewing as: **{st.session_state.view_as_workspace}**")
        st.divider()

    sync_workspace_from_disk()

    nav_options = ["📥 Connect Data", "📊 Raw Analysis", "🧩 Custom Builder", "⭐ Boss Dashboard", "🗂 Data Table",
                    "🤖 AI Assistant", "⚙️ Settings"]
    if st.session_state.role == auth.ROLE_REPORT_VIEWER:
        nav_options = ["⭐ Boss Dashboard"]   # nothing else exists for this account, not even Settings
    if st.session_state.role == auth.ROLE_ADMIN:
        nav_options.append("🔐 Admin Panel")
    page = st.radio("Navigate", nav_options, label_visibility="collapsed")
    st.session_state.page = page
    st.divider()
    if st.session_state.data_source_name:
        st.success(f"Loaded: {st.session_state.data_source_name}")
        if st.session_state.df_raw is not None:
            st.caption(f"{len(st.session_state.df_raw):,} rows × {len(st.session_state.df_raw.columns)} cols")
    else:
        st.info("No data loaded yet.")
    if not can_edit():
        st.caption("👁️ View-only account — you can look at reports here but not upload data or change dashboards.")
    st.divider()
    if st.button("🚪 Logout", use_container_width=True):
        auth.destroy_session(st.session_state.get("_session_token") or _get_session_cookie())  # invalidate server-side
        st.session_state.authenticated = False
        st.session_state.username = None
        st.session_state.role = None
        st.session_state.workspace_id = None
        st.session_state.view_as_workspace = None
        st.session_state._loaded_workspace_id = None
        st.session_state._session_token = None
        _clear_session_cookie()  # clears the cookie + reloads the page itself; nothing to do after this call


# ==================================================================================
# PAGE 0: CONNECT DATA (always the first step — file upload OR live database)
# ==================================================================================
if page == "📥 Connect Data":
    top_l, top_r = st.columns([5, 1])
    with top_l:
        st.title("📥 Connect Data")
        st.caption("Every other page (Raw Analysis, Boss Dashboard, Data Table) works off whatever dataset is "
                   "loaded here. Pick a source, load it once, then go build your analysis.")
    with top_r:
        st.write("")
        if st.button("🔄 Refresh", help="Re-check the currently loaded dataset / clear a stuck upload and start over",
                     use_container_width=True):
            st.session_state._last_upload_sig = None
            st.rerun()

    if st.session_state.df_raw is not None:
        st.success(f"🟢 Currently loaded: **{st.session_state.data_source_name}** "
                   f"({len(st.session_state.df_raw):,} rows × {len(st.session_state.df_raw.columns)} cols)")
        if can_edit() and st.button("🗑️ Clear loaded data (start over with a new file/database)"):
            st.session_state.df_raw = None
            st.session_state.meta = None
            st.session_state.data_source_name = None
            st.session_state.filters = {}
            st.session_state.dashboard_charts = []
            st.session_state.pinned_kpis = []
            st.session_state.dashboard_slicers = []
            st.session_state._last_upload_sig = None
            st.rerun()

    if can_edit():
        src_tab_file, src_tab_db = st.tabs(["📁 Upload File", "🔌 Connect Database"])

        # ---------------- FILE UPLOAD ----------------
        with src_tab_file:
            up_col, sample_col = st.columns([3, 1])
            with up_col:
                uploaded = st.file_uploader(
                    "Import data (CSV, XLSX, JSON, PDF) — pick multiple files to combine them into one dataset",
                    type=["csv", "tsv", "xlsx", "xls", "json", "pdf"], accept_multiple_files=True,
                )
                if uploaded:
                    if len(uploaded) > 1:
                        combine_label = st.radio(
                            "How should these files be combined?",
                            ["Stack rows (append — e.g. Jan.csv + Feb.csv + Mar.csv)",
                             "Paste side-by-side (same rows, different columns per file)"],
                            key="combine_mode_choice",
                        )
                        combine_mode = "stack" if combine_label.startswith("Stack") else "columns"
                        if st.button(f"🔗 Combine & Load {len(uploaded)} files", type="primary"):
                            if load_files(uploaded, combine_mode=combine_mode) is not None:
                                st.session_state._last_upload_sig = None  # multi-file combos aren't cheaply re-checked; just don't re-trigger single-file path
                                st.rerun()
                    else:
                        # Streamlit re-runs this whole script on every click anywhere on the page,
                        # and `uploaded` still holds the same file as long as it's sitting in the
                        # uploader — so without this guard, load_file() re-ran (and errored, since
                        # the file's stream was already consumed) on every single interaction, not
                        # just right after a genuine new upload.
                        sig = (uploaded[0].name, uploaded[0].size)
                        if st.session_state.get("_last_upload_sig") != sig:
                            if load_file(uploaded[0]) is not None:
                                st.session_state._last_upload_sig = sig
                                st.rerun()
            with sample_col:
                st.write("")
                st.write("")
                if st.button("🎯 Load Sample Data", use_container_width=True):
                    load_sample()
                    st.rerun()

        # ---------------- DATABASE CONNECT ----------------
        with src_tab_db:
            st.caption("Connect to a live SQL database and pick a table to load as your working dataset. "
                       "Read-only — only SELECT is ever run against your database.")
            if not dbc.SQLALCHEMY_AVAILABLE:
                st.warning("SQLAlchemy isn't installed in this environment yet. Add `sqlalchemy` (plus a driver "
                           "like `psycopg2-binary` for Postgres or `pymysql` for MySQL) to requirements.txt and reinstall.")

            with st.expander("🔌 Connection", expanded=not st.session_state.db_connected):
                db_type = st.selectbox("Database type", list(dbc.DB_TYPES.keys()),
                                        index=list(dbc.DB_TYPES.keys()).index(st.session_state.db_conn_type)
                                        if st.session_state.db_conn_type in dbc.DB_TYPES else 0,
                                        key="cd_db_type_pick")
                st.session_state.db_conn_type = db_type

                if db_type == "Custom SQLAlchemy URI":
                    uri = st.text_input("SQLAlchemy URI", placeholder="postgresql+psycopg2://user:pass@host:5432/dbname",
                                         key="cd_db_raw_uri")
                elif db_type == "SQLite (local file path)":
                    sqlite_path = st.text_input("SQLite file path", placeholder="/path/to/database.db", key="cd_db_sqlite_path")
                    uri = dbc.build_uri(db_type, database=sqlite_path)
                else:
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        host = st.text_input("Host", placeholder="localhost", key="cd_db_host")
                    with c2:
                        port = st.text_input("Port", placeholder=str(dbc.DB_TYPES[db_type]["default_port"]), key="cd_db_port")
                    with c3:
                        database = st.text_input("Database name", key="cd_db_name")
                    c4, c5 = st.columns(2)
                    with c4:
                        username = st.text_input("Username", key="cd_db_user")
                    with c5:
                        password = st.text_input("Password", type="password", key="cd_db_pass")
                    odbc_driver = ""
                    if db_type == "SQL Server":
                        odbc_driver = st.text_input("ODBC driver name", value="ODBC Driver 17 for SQL Server", key="cd_db_odbc")
                    uri = dbc.build_uri(db_type, host=host, port=port, database=database,
                                         username=username, password=password, odbc_driver=odbc_driver)

                cc1, cc2 = st.columns([1, 3])
                with cc1:
                    if st.button("🔗 Test & Connect", type="primary", use_container_width=True, key="cd_db_connect_btn"):
                        try:
                            dbc.test_connection(uri)
                            st.session_state.db_conn_uri = uri
                            st.session_state.db_connected = True
                            st.success("Connected successfully.")
                        except dbc.ConnectionError as e:
                            st.session_state.db_connected = False
                            st.error(f"Could not connect: {e}")
                with cc2:
                    if st.session_state.db_connected and st.button("🔌 Disconnect", key="cd_db_disconnect_btn"):
                        st.session_state.db_connected = False
                        st.session_state.db_conn_uri = ""
                        st.rerun()

            if st.session_state.db_connected:
                st.success(f"🟢 Connected — {st.session_state.db_conn_type}")
                tables = dbc.list_tables(st.session_state.db_conn_uri)
                if tables:
                    pick_table = st.selectbox("Table to load", tables, key="cd_db_table_pick")
                    load_mode = st.radio("Load", [f"Whole table ({pick_table})", "Custom SQL"], horizontal=True, key="cd_db_load_mode")
                    if load_mode.startswith("Custom"):
                        custom_sql = st.text_area("SQL query", value=f"SELECT * FROM {pick_table} LIMIT 5000", key="cd_db_custom_sql")
                    else:
                        custom_sql = f"SELECT * FROM {pick_table}"
                    if st.button("📥 Load this as my dataset", type="primary", key="cd_db_load_btn"):
                        try:
                            result_df = dbc.run_query(st.session_state.db_conn_uri, custom_sql)
                            cleaned = de.clean_dataframe(result_df)
                            _apply_loaded_df(cleaned, f"{st.session_state.db_conn_type}: {pick_table}")
                            st.success(f"Loaded {len(cleaned):,} rows from **{pick_table}**.")
                            st.rerun()
                        except dbc.QueryError as e:
                            st.error(f"Query failed: {e}")
                else:
                    st.info("No tables found in this database.")
    elif st.session_state.df_raw is None:
        st.info("No data has been uploaded for this account yet. Ask your admin/client to upload it or connect a database.")

    if st.session_state.df_raw is None:
        st.info("⬆️ Upload a file, connect a database, or click **Load Sample Data** to get started.")
        st.stop()
    else:
        st.divider()
        st.caption("Data loaded — head to **📊 Raw Analysis** in the sidebar to explore it.")
        st.stop()


# ==================================================================================
# PAGE 1: RAW ANALYSIS
# ==================================================================================
if page == "📊 Raw Analysis":
    st.title("📊 Raw Analysis")
    st.caption("Every KPI, chart and filter below is generated automatically from your loaded dataset's columns — nothing is hard-coded.")

    if st.session_state.df_raw is None:
        st.info("⬅️ No data loaded yet. Go to **📥 Connect Data** in the sidebar to upload a file or connect a database.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    df = render_filters(df_raw, meta, key_prefix="p1_")

    if df.empty:
        st.warning("No rows match the current filters.")
        st.stop()

    st.subheader("Key Performance Indicators")
    if can_edit():
        st.caption("Tick ⭐ under any card to pin it to the Boss Dashboard. Each card also has its own "
                   "**➕ Filter this card** — independent of the page filter above and of every other card.")
    with st.expander("🌐 Global number format (applies to every Total/Avg card at once)", expanded=False):
        fmt_labels = {"auto": "Auto (Cr / L / K)", "full": "Full number (no abbreviation)", "compact": "Compact (K / M / B)"}
        cur = st.session_state.p1_kpi_number_format
        choice = st.selectbox("Number format", list(fmt_labels.keys()), format_func=lambda k: fmt_labels[k],
                               index=list(fmt_labels.keys()).index(cur), key="p1_fmt_select")
        st.session_state.p1_kpi_number_format = choice
    kpis = de.compute_kpis(df, meta, number_format=st.session_state.p1_kpi_number_format)
    kpi_cards(kpis, pinnable=can_edit(), key_prefix="p1_", df=df, filterable=True,
              number_format=st.session_state.p1_kpi_number_format)

    st.divider()
    st.subheader("Chart Library — 10 variants per chart type")
    st.caption("Pick the analysis you like best per chart type with **⭐ Add to Boss Dashboard**. You can change it any time.")

    style = get_style_dict()
    tabs = st.tabs([f"{FAMILY_ICONS.get(f,'')} {f}" for f in ce.FAMILIES])
    for fam, tab in zip(ce.FAMILIES, tabs):
        with tab:
            variants = ce.generate_variants(df, meta, fam)
            if not variants:
                st.info(f"Not enough suitable columns in this dataset to build {fam} charts.")
                continue
            for row_start in range(0, len(variants), 2):
                cols = st.columns(2)
                for j, v in enumerate(variants[row_start:row_start + 2]):
                    with cols[j]:
                        v = customize_variant(fam, v, meta, key_prefix=f"p1_cz_{fam}_{v['id']}_")
                        fig, insight = ce.build_figure(df, v, style)
                        st.plotly_chart(fig, use_container_width=True, key=f"p1_{fam}_{v['id']}", config=ce.PLOTLY_CONFIG)
                        st.caption(f"💡 {insight}")
                        if can_edit():
                            already = chart_in_dashboard(fam, v["id"])
                            btn_label = "✅ On Dashboard" if already else "⭐ Add to Boss Dashboard"
                            if st.button(btn_label, key=f"p1_add_{fam}_{v['id']}", use_container_width=True, disabled=already):
                                add_to_dashboard(fam, v)
                                st.rerun()


# ==================================================================================
# PAGE 1.5: CUSTOM BUILDER (Power-BI style: pick your own column + measure + filters)
# ==================================================================================
elif page == "🧩 Custom Builder":
    st.title("🧩 Custom Builder")
    st.caption("Build your own KPI cards and charts — pick the column, the measure "
               "(Sum / Average / Count / Distinct Count / Min / Max ...), and filters "
               "that apply to that card or chart only. Just like Power BI field wells.")

    if st.session_state.df_raw is None:
        st.info("Load data on the **📥 Connect Data** page first.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    editable = can_edit()

    tab_kpi, tab_chart = st.tabs(["📇 Custom KPI Cards", "📐 Custom Charts"])

    # ---------------- CUSTOM KPI CARDS ----------------
    with tab_kpi:
        c1, c2 = st.columns([5, 1])
        with c1:
            st.caption("Every card below has its own column, measure and (optional) filters.")
        with c2:
            if editable and st.button("➕ New Card", use_container_width=True, key="add_kpi_card"):
                st.session_state.custom_kpis.append(be.new_kpi_card(df_raw))
                st.rerun()

        if editable and st.session_state.custom_kpis:
            be.render_global_kpi_toolbar(df_raw, st.session_state.custom_kpis, key_prefix="gkpi_")

        if not st.session_state.custom_kpis:
            st.info("No custom KPI cards yet." if not editable else "No custom KPI cards yet — click **➕ New Card** to build one.")
        else:
            for row_start in range(0, len(st.session_state.custom_kpis), 3):
                cols = st.columns(3)
                for j, card in enumerate(st.session_state.custom_kpis[row_start:row_start + 3]):
                    with cols[j]:
                        box = st.container(border=True)
                        with box:
                            be.render_kpi_card_value(df_raw, card)
                            if editable:
                                with st.expander("⚙️ Edit this card"):
                                    be.render_kpi_card_editor(df_raw, card, key_prefix="ckpi_")
                                    bc1, bc2 = st.columns(2)
                                    with bc1:
                                        card["pinned"] = st.checkbox("⭐ Pin to Boss Dashboard", value=card.get("pinned", False),
                                                                      key=f"ckpi_pin_{card['id']}")
                                    with bc2:
                                        if st.button("🗑️ Delete card", key=f"ckpi_del_{card['id']}"):
                                            st.session_state.custom_kpis = [c for c in st.session_state.custom_kpis if c["id"] != card["id"]]
                                            st.rerun()

    # ---------------- CUSTOM CHARTS ----------------
    with tab_chart:
        c1, c2 = st.columns([5, 1])
        with c1:
            st.caption("Pick chart type, X field, Y field + measure, an optional Color/Legend field, and per-chart filters.")
        with c2:
            if editable and st.button("➕ New Chart", use_container_width=True, key="add_chart"):
                st.session_state.custom_charts.append(be.new_chart(df_raw))
                st.rerun()

        if not st.session_state.custom_charts:
            st.info("No custom charts yet." if not editable else "No custom charts yet — click **➕ New Chart** to build one.")
        else:
            style = get_style_dict()
            for chart in list(st.session_state.custom_charts):
                box = st.container(border=True)
                with box:
                    top1, top2, top3 = st.columns([5, 2, 1])
                    with top1:
                        st.markdown(f"**{be.CHART_ICONS.get(chart['type'],'')} {chart['title']}**")
                    with top2:
                        if editable:
                            chart["pinned"] = st.checkbox("⭐ Pin to Boss Dashboard", value=chart.get("pinned", False),
                                                           key=f"cchart_pin_{chart['id']}")
                    with top3:
                        if editable and st.button("🗑️", key=f"cchart_del_{chart['id']}", help="Delete this chart"):
                            st.session_state.custom_charts = [c for c in st.session_state.custom_charts if c["id"] != chart["id"]]
                            st.rerun()

                    if editable:
                        with st.expander("⚙️ Field wells & filters", expanded=False):
                            be.render_chart_editor(df_raw, chart, key_prefix="cchart_")

                    fig, insight, table_df = be.build_custom_figure(df_raw, chart, style)
                    if table_df is not None:
                        st.dataframe(table_df, use_container_width=True, height=420)
                    elif fig is not None:
                        st.plotly_chart(fig, use_container_width=True, key=f"cchart_fig_{chart['id']}", config=ce.PLOTLY_CONFIG)
                    st.caption(f"💡 {insight}")


# ==================================================================================
# PAGE 2: BOSS DASHBOARD
# ==================================================================================
elif page == "⭐ Boss Dashboard":
    # Pull the latest saved state for this workspace every time this page runs -
    # a client and their linked report-viewer(s) are separate browser sessions
    # sharing one workspace, so this is what makes one person's change (pinned/
    # swapped charts, slicers, theme...) show up for the other. See the
    # docstring on sync_workspace_from_disk() for why this is safe.
    sync_workspace_from_disk(force=True)

    if can_edit_dashboard():
        name_col, _ = st.columns([3, 2])
        with name_col:
            new_name = st.text_input("Dashboard name", value=st.session_state.dashboard_name or "⭐ Boss Dashboard",
                                      label_visibility="collapsed", key="dashboard_name_input")
            if new_name and new_name.strip() and new_name != st.session_state.dashboard_name:
                st.session_state.dashboard_name = new_name
        st.title(st.session_state.dashboard_name or "⭐ Boss Dashboard")
    else:
        st.title(st.session_state.dashboard_name or "⭐ Boss Dashboard")
    st.caption("Only what you picked shows up here. Style it, swap any chart, then export a clean PDF.")

    # Auto-sync so a client and their linked report-viewer(s) - separate browser
    # sessions sharing this workspace - always see each other's latest changes
    # (pinned/swapped charts, slicers, theme...) without anyone touching a
    # button. No user-facing controls for this on purpose - it's just always on.
    if AUTOREFRESH_AVAILABLE:
        st_autorefresh(interval=1000, key="boss_dashboard_live_sync")

    if st.session_state.df_raw is None:
        st.info("Load data on the **📥 Connect Data** page first.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta

    with st.expander("🎨 Dashboard Theme & Style", expanded=False):
        th = st.session_state.theme
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            th["bg_color"] = st.color_picker("Page background", th["bg_color"])
        with c2:
            th["font_color"] = st.color_picker("Font color", th["font_color"])
        with c3:
            th["accent_color"] = st.color_picker("Accent / KPI color", th["accent_color"])
        with c4:
            th["palette_name"] = st.selectbox("Chart color palette", list(PALETTES.keys()),
                                               index=list(PALETTES.keys()).index(th["palette_name"]))
        c5, c6, c7, c8 = st.columns(4)
        with c5:
            th["font_family"] = st.selectbox("Chart font", ["Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana"],
                                              index=["Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana"].index(th["font_family"]) if th["font_family"] in ["Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana"] else 0)
        with c6:
            th["template"] = st.selectbox("Chart theme", ["plotly_white", "plotly_dark", "ggplot2", "seaborn", "simple_white"],
                                           index=["plotly_white", "plotly_dark", "ggplot2", "seaborn", "simple_white"].index(th["template"]) if th["template"] in ["plotly_white", "plotly_dark", "ggplot2", "seaborn", "simple_white"] else 0)
        with c7:
            th["show_legend"] = st.checkbox("Show legend", th["show_legend"])
        with c8:
            th["show_labels"] = st.checkbox("Show data labels", th["show_labels"])
        wallpaper = st.file_uploader("PDF background wallpaper (optional, PNG/JPG)", type=["png", "jpg", "jpeg"], key="wallpaper_up")
        if wallpaper is not None:
            th["wallpaper_bytes"] = _read_upload(wallpaper)
        if th.get("wallpaper_bytes") and st.button("Remove wallpaper"):
            th["wallpaper_bytes"] = None
        st.session_state.theme = th

    df = render_filters(df_raw, meta, key_prefix="p2_")
    df = render_slicers(df, meta, key_prefix="p2_", editable=can_edit_dashboard())
    style = get_style_dict()

    # ---- Pinned KPIs ----
    st.subheader("Key Performance Indicators")
    all_kpis = de.compute_kpis(df, meta)
    pinned = [k for k in all_kpis if k["label"] in st.session_state.pinned_kpis]
    pinned_custom_kpis = [c for c in st.session_state.custom_kpis if c.get("pinned")]
    if not pinned and not pinned_custom_kpis:
        st.info("No KPI cards pinned yet. Go to Raw Analysis and tick ⭐ under the KPI cards you want here, "
                "or build your own on the Custom Builder page.")
    else:
        if pinned:
            kpi_cards(pinned, pinnable=False, removable=can_edit_dashboard(), key_prefix="p2_")
        if pinned_custom_kpis:
            st.caption("🧩 Custom KPI cards")
            for row_start in range(0, len(pinned_custom_kpis), 4):
                cols = st.columns(4)
                for j, card in enumerate(pinned_custom_kpis[row_start:row_start + 4]):
                    with cols[j]:
                        be.render_kpi_card_value(df, card)
                        if can_edit_dashboard() and st.button("🗑️ Remove", key=f"p2_rm_custom_kpi_{card['id']}",
                                                      help="Unpin this card from the dashboard", use_container_width=True):
                            card["pinned"] = False
                            st.rerun()

    st.divider()

    # ---- Selected charts ----
    st.subheader("Selected Charts")
    pinned_custom_charts = [c for c in st.session_state.custom_charts if c.get("pinned")]
    chart_png_items = []
    st.session_state._pdf_render_errors = []

    if not st.session_state.dashboard_charts and not pinned_custom_charts:
        st.info("No charts selected yet. Go to Raw Analysis and click ⭐ Add to Boss Dashboard on any chart, "
                "or build & pin your own on the Custom Builder page.")
    else:
        for chart in pinned_custom_charts:
            box = st.container(border=True)
            with box:
                top1, top3 = st.columns([8, 1])
                with top1:
                    st.markdown(f"**🧩 {be.CHART_ICONS.get(chart['type'],'')} {chart['title']}**")
                with top3:
                    if can_edit_dashboard() and st.button("🗑️", key=f"rm_custom_{chart['id']}", help="Unpin from dashboard"):
                        chart["pinned"] = False
                        st.rerun()

                fig, insight, table_df = be.build_custom_figure(df, chart, style)
                png_bytes = None
                if table_df is not None:
                    st.dataframe(table_df, use_container_width=True, height=380)
                elif fig is not None:
                    st.plotly_chart(fig, use_container_width=True, key=f"p2_custom_{chart['id']}", config=ce.PLOTLY_CONFIG)
                    try:
                        png_bytes = fig.to_image(format="png", width=1400, height=700, scale=2)
                    except Exception as e:
                        png_bytes = None
                        st.session_state.setdefault("_pdf_render_errors", [])
                        st.session_state._pdf_render_errors.append(f"{chart.get('title','Custom Chart')}: {e}")
                st.caption(f"💡 {insight}")
                chart_png_items.append({"title": chart.get("title", "Custom Chart"), "insight": insight,
                                         "png_bytes": png_bytes, "type": chart.get("type", "Chart")})

        for idx, entry in enumerate(list(st.session_state.dashboard_charts)):
            fam = entry["family"]
            variant = entry["variant"]
            all_variants = ce.generate_variants(df, meta, fam)
            id_to_variant = {v["id"]: v for v in all_variants}
            options = list(id_to_variant.keys())
            labels = {vid: id_to_variant[vid]["title"] for vid in options}
            widget_key = f"{fam}_{idx}_{variant['id']}"

            box = st.container(border=True)
            with box:
                top1, top2, top3 = st.columns([5, 3, 1])
                with top1:
                    st.markdown(f"**{FAMILY_ICONS.get(fam,'')} {fam}**")
                with top2:
                    if options and can_edit_dashboard():
                        default_idx = options.index(variant["id"]) if variant["id"] in options else 0
                        chosen_id = st.selectbox("Swap analysis", options, index=default_idx,
                                                  format_func=lambda x: labels.get(x, x),
                                                  key=f"swap_{widget_key}", label_visibility="collapsed")
                        variant = id_to_variant[chosen_id]
                        entry["variant"] = variant
                with top3:
                    if can_edit_dashboard() and st.button("🗑️", key=f"rm_{widget_key}", help="Remove from dashboard"):
                        remove_from_dashboard(fam, entry["variant"]["id"])
                        st.rerun()

                fig, insight = ce.build_figure(df, variant, style)
                st.plotly_chart(fig, use_container_width=True, key=f"p2_{widget_key}", config=ce.PLOTLY_CONFIG)
                st.caption(f"💡 {insight}")

                try:
                    png_bytes = fig.to_image(format="png", width=1400, height=700, scale=2)
                except Exception as e:
                    png_bytes = None
                    st.session_state.setdefault("_pdf_render_errors", [])
                    st.session_state._pdf_render_errors.append(f"{variant.get('title', fam)}: {e}")
                chart_png_items.append({"title": variant.get("title", fam), "insight": insight,
                                         "png_bytes": png_bytes, "type": fam})

        st.divider()
        st.subheader("📄 Export")
        if st.session_state.get("_pdf_render_errors"):
            with st.expander(f"⚠️ {len(st.session_state._pdf_render_errors)} chart(s) could not be rendered "
                              f"as images and will be missing from the PDF — click to see why"):
                for msg in st.session_state._pdf_render_errors:
                    st.caption(msg)
        report_title = st.text_input("Report title", st.session_state.dashboard_name or "Sports Performance & Payments Report")
        subtitle = st.text_input("Subtitle", f"Prepared for management review — {pd.Timestamp.today().date()}")
        filters_summary = ", ".join(
            [f"{k.replace('p2_','')}: {v}" for k, v in st.session_state.filters.items()
             if k.startswith("p2_") and v not in (None, [], ())]
        ) or "None"

        if st.button("⬇️ Generate & Download PDF", type="primary"):
            with st.spinner("Building PDF report..."):
                pdf_theme = {
                    "bg_color": st.session_state.theme["bg_color"],
                    "font_color": st.session_state.theme["font_color"],
                    "accent_color": st.session_state.theme["accent_color"],
                    "font_name": st.session_state.theme["font_name"],
                    "wallpaper_bytes": st.session_state.theme.get("wallpaper_bytes"),
                }
                custom_kpi_rows = []
                for card in pinned_custom_kpis:
                    fdf_card = ms.apply_filters(df, card.get("filters", []))
                    value, sub = ms.compute_measure(fdf_card, card["column"], card["measure"])
                    custom_kpi_rows.append({"label": card["title"], "value": ms.fmt_measure_value(value), "sub": sub})

                pdf_kpis = (pinned if pinned else (all_kpis[:8] if not pinned_custom_kpis else []))
                pdf_kpis = pdf_kpis + custom_kpi_rows

                pdf_bytes = pe.build_pdf_report(
                    report_title=report_title,
                    subtitle=subtitle,
                    kpis=pdf_kpis,
                    chart_items=[c for c in chart_png_items if c["png_bytes"]],
                    theme=pdf_theme,
                    filters_summary=filters_summary,
                )
            st.download_button("📥 Click to download report.pdf", data=pdf_bytes,
                                file_name="sports_analytics_report.pdf", mime="application/pdf",
                                type="primary")


# ==================================================================================
# PAGE 3: DATA TABLE
# ==================================================================================
elif page == "🗂 Data Table":
    st.title("🗂 Data Table")

    if st.session_state.df_raw is None:
        st.info("Load data on the **📥 Connect Data** page first.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta

    tab_simple, tab_sql, tab_db = st.tabs(["🖱️ Simple", "🖥️ SQL Query", "🔌 Database Connector"])

    # ---------------- SIMPLE (column picker + filters + sort) ----------------
    with tab_simple:
        st.caption("Choose your columns, filter, sort — like a simple SQL SELECT — then export.")
        df = render_filters(df_raw, meta, key_prefix="p3_")

        all_cols = list(df.columns)
        sel_cols = st.multiselect("Columns to display (SELECT ...)", all_cols, default=all_cols, key="p3_sel_cols")

        c1, c2, c3 = st.columns([2, 1, 1])
        with c1:
            query_str = st.text_input("Advanced filter — pandas query syntax (optional, like SQL WHERE)",
                                       placeholder="e.g. Amount > 100000 and Status == 'Paid'", key="p3_query_str")
        with c2:
            sort_col = st.selectbox("Sort by", ["(none)"] + all_cols, key="p3_sort_col")
        with c3:
            sort_dir = st.selectbox("Direction", ["Descending", "Ascending"], key="p3_sort_dir")

        row_limit = st.slider("Rows to display (for performance on very large files)", 100, 20000, 2000, step=100, key="p3_row_limit")

        view = df[sel_cols] if sel_cols else df
        if query_str:
            try:
                mask = df.eval(query_str)
                view = view[mask]
            except Exception as e:
                st.warning(f"Could not apply that filter: {e}")

        if sort_col != "(none)" and sort_col in df.columns:
            view = view.assign(**{"_sortkey": df.loc[view.index, sort_col]}).sort_values(
                "_sortkey", ascending=(sort_dir == "Ascending")).drop(columns=["_sortkey"])

        st.caption(f"Showing {min(len(view), row_limit):,} of {len(view):,} matching rows (out of {len(df):,} total).")
        st.dataframe(view.head(row_limit), use_container_width=True, height=560)

        csv_bytes = view.to_csv(index=False).encode("utf-8")
        st.download_button("⬇️ Download filtered data as CSV", csv_bytes, file_name="filtered_data.csv",
                            mime="text/csv", key="p3_simple_dl")

    # ---------------- SQL QUERY (real SQL via DuckDB) ----------------
    with tab_sql:
        st.caption("Real SQL (SELECT, WHERE, GROUP BY, computed columns, ORDER BY, window functions, CTEs...) "
                   "against the full loaded dataset — runs on its own, independent of the Simple tab's filters "
                   "above. Read-only: no INSERT/UPDATE/DELETE/file access.")
        st.code(f"Table name: {qe.DEFAULT_TABLE_NAME}   —   columns: {', '.join(df_raw.columns)}", language=None)

        default_sql = f"SELECT * FROM {qe.DEFAULT_TABLE_NAME} LIMIT 100"
        sql_text = st.text_area("SQL query", value=st.session_state.get("p3_sql_text", default_sql), height=140, key="p3_sql_text")

        run_col, _ = st.columns([1, 5])
        with run_col:
            run_clicked = st.button("▶️ Run query", type="primary", use_container_width=True, key="p3_sql_run")

        if run_clicked:
            try:
                result_df = qe.run_sql(df_raw, sql_text)
                st.session_state["p3_sql_result"] = result_df
                st.session_state["p3_sql_error"] = None
            except qe.QueryError as e:
                st.session_state["p3_sql_result"] = None
                st.session_state["p3_sql_error"] = str(e)

        if st.session_state.get("p3_sql_error"):
            st.error(st.session_state["p3_sql_error"])
        result_df = st.session_state.get("p3_sql_result")
        if result_df is not None:
            capped = len(result_df) >= qe.MAX_ROWS
            st.caption(f"{len(result_df):,} row(s) returned" + (f" (capped at {qe.MAX_ROWS:,})" if capped else "."))
            st.dataframe(result_df, use_container_width=True, height=560)
            sql_csv_bytes = result_df.to_csv(index=False).encode("utf-8")
            st.download_button("⬇️ Download query result as CSV", sql_csv_bytes, file_name="query_result.csv",
                                mime="text/csv", key="p3_sql_dl")

    # ---------------- EXTERNAL DATABASE CONNECTOR ----------------
    with tab_db:
        st.caption("Connect to ANY external SQL database (PostgreSQL, MySQL/MariaDB, SQL Server, SQLite, or a "
                   "custom SQLAlchemy URI for anything else) — independent of the file loaded on Raw Analysis. "
                   "Read-only: only SELECT/WITH queries are allowed. Connection details live only in this "
                   "browser session and are never written to disk.")

        if not dbc.SQLALCHEMY_AVAILABLE:
            st.warning("SQLAlchemy isn't installed in this environment yet. Add `sqlalchemy` (plus a driver "
                       "like `psycopg2-binary` for Postgres or `pymysql` for MySQL) to requirements.txt and reinstall.")

        if not can_edit():
            st.caption("👁️ View-only account — database connections are managed by your admin/client.")

        with st.expander("🔌 Connection", expanded=not st.session_state.db_connected):
            if can_edit():
                db_type = st.selectbox("Database type", list(dbc.DB_TYPES.keys()),
                                        index=list(dbc.DB_TYPES.keys()).index(st.session_state.db_conn_type)
                                        if st.session_state.db_conn_type in dbc.DB_TYPES else 0,
                                        key="db_type_pick")
                st.session_state.db_conn_type = db_type

                if db_type == "Custom SQLAlchemy URI":
                    raw_uri = st.text_input("SQLAlchemy URI", placeholder="postgresql+psycopg2://user:pass@host:5432/dbname",
                                             key="db_raw_uri")
                    uri = raw_uri
                elif db_type == "SQLite (local file path)":
                    sqlite_path = st.text_input("SQLite file path", placeholder="/path/to/database.db", key="db_sqlite_path")
                    uri = dbc.build_uri(db_type, database=sqlite_path)
                else:
                    c1, c2, c3 = st.columns(3)
                    with c1:
                        host = st.text_input("Host", placeholder="localhost", key="db_host")
                    with c2:
                        port = st.text_input("Port", placeholder=str(dbc.DB_TYPES[db_type]["default_port"]), key="db_port")
                    with c3:
                        database = st.text_input("Database name", key="db_name")
                    c4, c5 = st.columns(2)
                    with c4:
                        username = st.text_input("Username", key="db_user")
                    with c5:
                        password = st.text_input("Password", type="password", key="db_pass")
                    odbc_driver = ""
                    if db_type == "SQL Server":
                        odbc_driver = st.text_input("ODBC driver name", value="ODBC Driver 17 for SQL Server", key="db_odbc")
                    uri = dbc.build_uri(db_type, host=host, port=port, database=database,
                                         username=username, password=password, odbc_driver=odbc_driver)

                cc1, cc2 = st.columns([1, 3])
                with cc1:
                    if st.button("🔗 Test & Connect", type="primary", use_container_width=True, key="db_connect_btn"):
                        try:
                            dbc.test_connection(uri)
                            st.session_state.db_conn_uri = uri
                            st.session_state.db_connected = True
                            if not st.session_state.db_queries:
                                st.session_state.db_queries = [dbc.new_query_tab("Query 1")]
                            st.success("Connected successfully.")
                        except dbc.ConnectionError as e:
                            st.session_state.db_connected = False
                            st.error(f"Could not connect: {e}")
                with cc2:
                    if st.session_state.db_connected and st.button("🔌 Disconnect", key="db_disconnect_btn"):
                        st.session_state.db_connected = False
                        st.session_state.db_conn_uri = ""
                        st.session_state.db_queries = []
                        st.session_state.db_query_results = {}
                        st.rerun()

                if st.session_state.db_connected:
                    tables = dbc.list_tables(st.session_state.db_conn_uri)
                    if tables:
                        st.caption("Tables available: " + ", ".join(tables[:40]) + (" ..." if len(tables) > 40 else ""))
            else:
                st.caption("Connected." if st.session_state.db_connected else "No database connected yet.")

        if not st.session_state.db_connected:
            st.info("Connect to a database above to run live SQL queries against it.")
        else:
            st.success(f"🟢 Connected — {st.session_state.db_conn_type}")

            if can_edit():
                add_col, _ = st.columns([1, 4])
                with add_col:
                    if st.button("➕ New query tab", key="db_add_query_tab"):
                        n = len(st.session_state.db_queries) + 1
                        st.session_state.db_queries.append(dbc.new_query_tab(f"Query {n}"))
                        st.rerun()

            if not st.session_state.db_queries:
                st.info("No query tabs yet — click **➕ New query tab** to add one.")
            else:
                q_tabs = st.tabs([q["name"] for q in st.session_state.db_queries])
                for q, q_tab in zip(st.session_state.db_queries, q_tabs):
                    with q_tab:
                        qid = q["id"]
                        if can_edit():
                            hc1, hc2 = st.columns([4, 1])
                            with hc1:
                                q["name"] = st.text_input("Tab name", q["name"], key=f"db_qname_{qid}")
                            with hc2:
                                st.write("")
                                if len(st.session_state.db_queries) > 1 and st.button("🗑️ Remove tab", key=f"db_qdel_{qid}"):
                                    st.session_state.db_queries = [x for x in st.session_state.db_queries if x["id"] != qid]
                                    st.session_state.db_query_results.pop(qid, None)
                                    st.rerun()

                            q["sql"] = st.text_area("SQL query", value=q["sql"], height=120, key=f"db_qsql_{qid}")

                            rc1, rc2, rc3 = st.columns([1, 1, 2])
                            with rc1:
                                run_now = st.button("▶️ Run", type="primary", key=f"db_qrun_{qid}", use_container_width=True)
                            with rc2:
                                q["auto_refresh"] = st.checkbox("Auto-refresh", value=q.get("auto_refresh", False), key=f"db_qauto_{qid}")
                            with rc3:
                                q["refresh_interval_sec"] = st.slider("Interval (seconds)", 5, 300,
                                                                       q.get("refresh_interval_sec", 30),
                                                                       key=f"db_qint_{qid}", disabled=not q["auto_refresh"])
                        else:
                            run_now = False
                            st.code(q["sql"], language="sql")

                        # Auto-refresh: re-runs this script on the chosen interval while this tab's
                        # toggle is on, so the query result stays live without any manual clicking.
                        if q.get("auto_refresh") and AUTOREFRESH_AVAILABLE:
                            st_autorefresh(interval=q.get("refresh_interval_sec", 30) * 1000, key=f"db_ar_{qid}")
                        elif q.get("auto_refresh") and not AUTOREFRESH_AVAILABLE:
                            st.caption("⚠️ Install `streamlit-autorefresh` to enable live auto-refresh — running once for now.")

                        due_for_auto_run = q.get("auto_refresh") and (time.time() - q.get("last_run_ts", 0) >= q.get("refresh_interval_sec", 30))
                        if run_now or due_for_auto_run:
                            try:
                                result_df = dbc.run_query(st.session_state.db_conn_uri, q["sql"])
                                st.session_state.db_query_results[qid] = result_df
                                q["error"] = None
                                q["last_run_ts"] = time.time()
                                q["row_count"] = len(result_df)
                            except dbc.QueryError as e:
                                q["error"] = str(e)

                        if q.get("error"):
                            st.error(q["error"])
                        result_df = st.session_state.db_query_results.get(qid)
                        if result_df is not None:
                            last_run = time.strftime("%H:%M:%S", time.localtime(q.get("last_run_ts", time.time())))
                            st.caption(f"{len(result_df):,} row(s) · last run at {last_run}"
                                       + (" · 🔄 auto-refreshing" if q.get("auto_refresh") else ""))
                            st.dataframe(result_df, use_container_width=True, height=420)
                            st.download_button("⬇️ Download as CSV", result_df.to_csv(index=False).encode("utf-8"),
                                                file_name=f"{q['name'].replace(' ', '_')}.csv", mime="text/csv",
                                                key=f"db_qdl_{qid}")
                        elif not q.get("error"):
                            st.caption("Not run yet — click ▶️ Run.")


# ==================================================================================
# PAGE 3.5: AI ASSISTANT — free chat with your data (OpenRouter free tier)
# ==================================================================================
elif page == "🤖 AI Assistant":
    st.title("🤖 AI Assistant")
    st.caption("Ask questions in plain language about your data, KPIs, and dashboard charts. Answers are "
               "grounded in real SQL run against your dataset — not guesses — and shown with proof below each reply.")

    if st.session_state.df_raw is None:
        st.info("Load data on the **📥 Connect Data** page first.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    api_key = ac.get_api_key() or st.session_state.ai_groq_key

    if not api_key:
        if st.session_state.role == auth.ROLE_ADMIN:
            # Only the admin ever sees a way to type a key in the UI — clients/viewers
            # never see this box, never see the key, and can't set their own.
            st.warning("No free OpenRouter API key configured yet.")
            with st.expander("🆓 How to get a free key (2 minutes, no credit card)", expanded=True):
                st.markdown(
                    "1. Go to **[openrouter.ai](https://openrouter.ai)** and sign up (free).\n"
                    "2. Open **Keys** → **Create Key** → copy it (starts with `sk-or-v1-...`).\n"
                    "3. Paste it below to test it now — kept only in this browser session, never saved to disk.\n\n"
                    "**To turn the chatbot on for every client permanently** (so they never see this screen "
                    "or the key), set it once on the server as an environment variable `OPENROUTER_API_KEY`, or add "
                    "it to `.streamlit/secrets.toml` — see README. Clients then just get a working chatbot, "
                    "with no key ever shown or editable on their side.\n\n"
                    "*(Switched from Groq to OpenRouter because Groq's free tier blocks requests from "
                    "cloud-hosted servers like Streamlit Cloud — OpenRouter doesn't have that restriction.)*"
                )
            typed_key = st.text_input("OpenRouter API key (admin only, this session)", type="password", key="ai_key_input")
            if st.button("Save key for this session", type="primary") and typed_key:
                st.session_state.ai_groq_key = typed_key
                st.rerun()
        else:
            st.info("🤖 AI Assistant abhi enable nahi hai. Please contact your admin to turn this on.")
        st.stop()

    kpis = de.compute_kpis(df_raw, meta)

    for turn in st.session_state.ai_chat_history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("proof_df") is not None:
                with st.expander("🔍 Proof (SQL + data used)"):
                    st.code(turn["sql_used"], language="sql")
                    st.dataframe(turn["proof_df"], use_container_width=True)

    question = st.chat_input("e.g. \"Which record is number 5?\" or \"What's the trend over the last 3 months?\"")
    if question:
        st.session_state.ai_chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("Analysing your data..."):
                result = ac.ask(question, df_raw, meta, kpis, st.session_state.dashboard_charts,
                                 api_key, history=st.session_state.ai_chat_history[:-1])
            if result["error"]:
                st.error(result["error"])
            else:
                st.markdown(result["answer"])
                if result["proof_df"] is not None:
                    with st.expander("🔍 Proof (SQL + data used)"):
                        st.code(result["sql_used"], language="sql")
                        st.dataframe(result["proof_df"], use_container_width=True)
                st.session_state.ai_chat_history.append({
                    "role": "assistant", "content": result["answer"],
                    "sql_used": result["sql_used"], "proof_df": result["proof_df"],
                })

    if st.session_state.ai_chat_history and st.button("🗑️ Clear chat"):
        st.session_state.ai_chat_history = []
        st.rerun()


# ==================================================================================
# PAGE 4: SETTINGS
# ==================================================================================
elif page == "⚙️ Settings":
    st.title("⚙️ Settings")

    tab_names = ["🎨 Defaults", "🔑 My Account"]
    if st.session_state.role == auth.ROLE_CLIENT:
        tab_names.append("👥 My Report Viewers")
    tab_names.append("ℹ️ How This Tool Works")
    tabs = st.tabs(tab_names)
    tab_defaults, tab_account = tabs[0], tabs[1]
    tab_report_viewers = tabs[2] if st.session_state.role == auth.ROLE_CLIENT else None
    tab_about = tabs[-1]

    with tab_account:
        st.subheader("Change my password")
        st.caption("Only changes **your own** login — no one else's password or data is affected, "
                   "and your admin can still reset your password if you ever get locked out.")
        with st.form("self_change_pw"):
            cur_pw = st.text_input("Current password", type="password", key="self_cur_pw")
            np1 = st.text_input("New password", type="password", key="self_new_pw1")
            np2 = st.text_input("Confirm new password", type="password", key="self_new_pw2")
            submit = st.form_submit_button("Update my password")
            if submit:
                if not auth.verify_login(st.session_state.username, cur_pw):
                    st.error("Current password is incorrect.")
                elif not np1 or np1 != np2:
                    st.error("New passwords are empty or don't match.")
                else:
                    auth.change_password(st.session_state.username, np1)
                    st.success("Password updated. Use it next time you log in.")

        st.divider()
        st.subheader("My email (for 'Forgot password' recovery)")
        cur_email = auth.get_email(st.session_state.username) or ""
        st.caption("If you ever forget your password, a reset link is sent to this address — "
                   "keep it up to date. Nobody but you gets emailed here.")
        with st.form("self_set_email"):
            new_email = st.text_input("Email address", value=cur_email)
            if st.form_submit_button("Save email"):
                auth.set_email(st.session_state.username, new_email)
                st.success("Email saved.")

    if tab_report_viewers is not None:
        with tab_report_viewers:
            st.subheader("Give your boss/manager their own login")
            st.caption(
                "Creates a **Report Viewer** account, locked to only your data. They can log in from "
                "anywhere (no need to be in the same room as you) and will see **only the Boss "
                "Dashboard** — nothing else in this app. There they get full control: view everything, "
                "**export PDF**, and **manage slicers** (add/remove/change which filter fields show). "
                "They can never see your other data, other clients' data, or reach any other page. "
                "Your admin can always see and manage these accounts too."
            )
            my_report_viewers = [u for u in auth.list_users()
                                  if u["role"] == auth.ROLE_REPORT_VIEWER
                                  and u["workspace_id"] == st.session_state.workspace_id]
            if my_report_viewers:
                st.table([{"Username": u["username"], "Email": u["email"] or "—"} for u in my_report_viewers])

            st.divider()
            st.subheader("Create a new Report Viewer login")
            with st.form("client_create_report_viewer"):
                rv_u = st.text_input("Username")
                rv_p1 = st.text_input("Password", type="password")
                rv_p2 = st.text_input("Confirm password", type="password")
                rv_email = st.text_input("Their email (optional, needed only for their own 'Forgot password')")
                if st.form_submit_button("Create login", type="primary"):
                    if not rv_u.strip() or not rv_p1:
                        st.error("Username and password cannot be empty.")
                    elif rv_p1 != rv_p2:
                        st.error("Passwords do not match.")
                    elif auth.user_exists(rv_u.strip()):
                        st.error(f"'{rv_u.strip()}' is already taken — pick another username.")
                    else:
                        # workspace_id is forced to YOUR OWN workspace — a client can never grant
                        # a report-viewer login access to anyone else's data, even by mistake.
                        auth.create_or_update_user(rv_u.strip(), rv_p1, auth.ROLE_REPORT_VIEWER,
                                                    workspace_id=st.session_state.workspace_id, email=rv_email.strip())
                        st.success(f"'{rv_u.strip()}' can now log in and will see your Boss Dashboard.")
                        st.rerun()

            if my_report_viewers:
                st.divider()
                st.subheader("Reset a password / remove access")
                sel_rv = st.selectbox("Account", [u["username"] for u in my_report_viewers])
                rc1, rc2 = st.columns(2)
                with rc1:
                    with st.form("client_reset_rv_pw"):
                        nrp1 = st.text_input("New password", type="password", key="nrp1")
                        nrp2 = st.text_input("Confirm new password", type="password", key="nrp2")
                        if st.form_submit_button("Reset their password"):
                            if not nrp1 or nrp1 != nrp2:
                                st.error("Passwords are empty or don't match.")
                            else:
                                auth.change_password(sel_rv, nrp1)
                                st.success(f"Password reset for '{sel_rv}'.")
                with rc2:
                    st.write("")
                    st.write("")
                    if st.button(f"🗑️ Remove '{sel_rv}' access", use_container_width=True):
                        try:
                            auth.delete_user(sel_rv, st.session_state.username)
                            st.success(f"'{sel_rv}' can no longer log in.")
                            st.rerun()
                        except ValueError as e:
                            st.error(str(e))

    with tab_defaults:
        if st.session_state.role == auth.ROLE_ADMIN:
            st.subheader("App Branding")
            st.caption("The title shown at the top of the sidebar for **every account** on this app "
                       "(not per-workspace - admin-only).")
            b = st.session_state.app_brand
            bc1, bc2 = st.columns([2, 1])
            with bc1:
                b["text"] = st.text_input("Brand text", b["text"], key="brand_text")
            with bc2:
                b["color"] = st.color_picker("Text color", b["color"], key="brand_color")
            bc3, bc4, bc5, bc6 = st.columns(4)
            with bc3:
                b["font_size"] = st.slider("Font size (px)", 12, 40, b["font_size"], key="brand_font_size")
            with bc4:
                font_opts = ["sans-serif", "serif", "monospace"]
                b["font_family"] = st.selectbox("Font family", font_opts,
                                                 index=font_opts.index(b["font_family"]) if b["font_family"] in font_opts else 0,
                                                 key="brand_font_family")
            with bc5:
                b["bold"] = st.checkbox("Bold", b["bold"], key="brand_bold")
            with bc6:
                b["italic"] = st.checkbox("Italic", b["italic"], key="brand_italic")

            st.markdown("**Logo** — shown on the login page and in the sidebar, for every account. "
                        "Only admins can change it (this whole section is inside the admin-only check above). "
                        "Set both a light-theme and dark-theme version so it looks right either way — the "
                        "correct one is picked automatically for each viewer.")
            logo_light_col, logo_dark_col = st.columns(2)
            with logo_light_col:
                st.caption("☀️ For **light** theme (use dark-colored logo art)")
                lt_up, lt_url = st.tabs(["📁 Upload", "🔗 Link"])
                with lt_up:
                    up_l = st.file_uploader("PNG, JPG or JPEG", type=["png", "jpg", "jpeg"], key="brand_logo_light_upload")
                    if up_l is not None:
                        b["logo_light_bytes"] = up_l.getvalue()
                        b["logo_light_url"] = ""  # an upload always takes priority — clear the URL so there's no ambiguity
                with lt_url:
                    url_l = st.text_input("Image URL", b.get("logo_light_url", ""), key="brand_logo_light_url")
                    if url_l != b.get("logo_light_url", ""):
                        b["logo_light_url"] = url_l
                        if url_l:
                            b["logo_light_bytes"] = None
                if b.get("logo_light_bytes") or b.get("logo_light_url", "").strip():
                    st.image(b.get("logo_light_bytes") or b.get("logo_light_url"), width=160)
                    if st.button("🗑️ Remove", key="brand_logo_light_remove"):
                        b["logo_light_bytes"] = None
                        b["logo_light_url"] = ""
                        st.rerun()
            with logo_dark_col:
                st.caption("🌙 For **dark** theme (use light/white-colored logo art)")
                dk_up, dk_url = st.tabs(["📁 Upload", "🔗 Link"])
                with dk_up:
                    up_d = st.file_uploader("PNG, JPG or JPEG", type=["png", "jpg", "jpeg"], key="brand_logo_dark_upload")
                    if up_d is not None:
                        b["logo_dark_bytes"] = up_d.getvalue()
                        b["logo_dark_url"] = ""
                with dk_url:
                    url_d = st.text_input("Image URL", b.get("logo_dark_url", ""), key="brand_logo_dark_url")
                    if url_d != b.get("logo_dark_url", ""):
                        b["logo_dark_url"] = url_d
                        if url_d:
                            b["logo_dark_bytes"] = None
                if b.get("logo_dark_bytes") or b.get("logo_dark_url", "").strip():
                    st.image(b.get("logo_dark_bytes") or b.get("logo_dark_url"), width=160)
                    if st.button("🗑️ Remove", key="brand_logo_dark_remove"):
                        b["logo_dark_bytes"] = None
                        b["logo_dark_url"] = ""
                        st.rerun()
            st.caption("Tip: PNGs with a **transparent background** look best - they blend into either theme "
                       "instead of showing a white/black box around the logo.")

            lc1, lc2, lc3 = st.columns([2, 1, 1])
            with lc1:
                b["logo_width"] = st.slider("Logo width (px, login page)", 60, 400, b.get("logo_width", 220), key="brand_logo_width")
            with lc2:
                b["logo_glow"] = st.checkbox("✨ Blue glow animation", b.get("logo_glow", True), key="brand_logo_glow")
            with lc3:
                b["hide_login_title"] = st.checkbox("Replace login title text", b.get("hide_login_title", True),
                                                      key="brand_hide_login_title",
                                                      help="When a logo is set, show it instead of the "
                                                           "'Sports Analytics Platform' text heading on the login page.")

            if has_brand_logo(b):
                st.caption("Live preview (glow + auto theme-swap included):")
                _render_brand_logo(b)
            else:
                st.caption("No logo set — only the text title above shows.")

            st.markdown(
                f"<div style='font-size:{b['font_size']}px; color:{b['color']}; "
                f"font-weight:{'700' if b['bold'] else '400'}; "
                f"font-style:{'italic' if b['italic'] else 'normal'}; "
                f"font-family:{b['font_family']};'>Preview: {b['text']}</div>",
                unsafe_allow_html=True,
            )
            bcol_save, bcol_reset = st.columns([1, 1])
            with bcol_save:
                if st.button("💾 Save branding for everyone", type="primary", key="brand_save"):
                    st.session_state.app_brand = b
                    ws.save_branding(b)
                    st.success("Branding saved - every account will see it from their next run.")
            with bcol_reset:
                if st.button("Reset branding to default", key="brand_reset"):
                    st.session_state.app_brand = copy.deepcopy(DEFAULT_BRAND)
                    ws.save_branding(st.session_state.app_brand)
                    st.rerun()
            st.divider()

        st.subheader("Default Theme")
        st.caption("These are the starting values used on the Boss Dashboard — you can still override anything per-report. "
                   "(This used to be a raw JSON dump — every value below is now a normal, editable control.)")
        if st.button("Reset theme to factory defaults"):
            st.session_state.theme = copy.deepcopy(DEFAULT_THEME)
            st.rerun()

        th = st.session_state.theme
        d1, d2, d3, d4 = st.columns(4)
        with d1:
            th["bg_color"] = st.color_picker("Page background", th["bg_color"], key="set_bg_color")
        with d2:
            th["panel_color"] = st.color_picker("Panel background", th["panel_color"], key="set_panel_color")
        with d3:
            th["font_color"] = st.color_picker("Font color", th["font_color"], key="set_font_color")
        with d4:
            th["accent_color"] = st.color_picker("Accent / KPI color", th["accent_color"], key="set_accent_color")

        d5, d6, d7, d8 = st.columns(4)
        font_choices = ["Arial", "Helvetica", "Times New Roman", "Courier New", "Verdana"]
        with d5:
            th["font_family"] = st.selectbox("Chart font", font_choices,
                                              index=font_choices.index(th["font_family"]) if th["font_family"] in font_choices else 0,
                                              key="set_font_family")
        with d6:
            th["font_name"] = st.selectbox("PDF font", ["Helvetica", "Times-Roman", "Courier"],
                                            index=["Helvetica", "Times-Roman", "Courier"].index(th["font_name"]) if th["font_name"] in ["Helvetica", "Times-Roman", "Courier"] else 0,
                                            key="set_font_name")
        with d7:
            th["font_size"] = st.slider("Chart font size", 8, 24, th["font_size"], key="set_font_size")
        with d8:
            th["palette_name"] = st.selectbox("Chart color palette", list(PALETTES.keys()),
                                               index=list(PALETTES.keys()).index(th["palette_name"]),
                                               key="set_palette_name")

        d9, d10, d11, d12 = st.columns(4)
        template_choices = ["plotly_white", "plotly_dark", "ggplot2", "seaborn", "simple_white"]
        with d9:
            th["template"] = st.selectbox("Chart theme", template_choices,
                                           index=template_choices.index(th["template"]) if th["template"] in template_choices else 0,
                                           key="set_template")
        with d10:
            th["show_legend"] = st.checkbox("Show legend", th["show_legend"], key="set_show_legend")
        with d11:
            th["show_labels"] = st.checkbox("Show data labels", th["show_labels"], key="set_show_labels")
        with d12:
            st.write("")

        st.caption("🎨 Palette preview:")
        st.markdown(
            " ".join(f"<span style='background:{c};padding:6px 14px;border-radius:4px;margin-right:4px;'>&nbsp;</span>"
                     for c in PALETTES.get(th["palette_name"], PALETTES["Set2"])),
            unsafe_allow_html=True,
        )

        if th.get("wallpaper_bytes"):
            st.info("A PDF background wallpaper is currently set (manage it from the Boss Dashboard page).")

        st.session_state.theme = th

    with tab_about:
        st.subheader("What this tool does")
        # NOTE: this tab is shown to every role (client / viewer / report
        # viewer / admin). Nothing about the Admin Panel - or that one even
        # exists - belongs here. That description lives ONLY inside the
        # Admin Panel page itself, gated to role == admin. Keep it that way.
        st.markdown("""
**Sports Analytics Platform** turns any spreadsheet-shaped file into a boardroom-ready
report, without you writing a single formula.

**📊 Raw Analysis (Page 1)**
- Import CSV, XLSX, JSON or PDF (table-based PDFs). Works on *any* dataset — sports,
  sales, HR, finance — because columns are detected automatically, not hard-coded.
- KPI cards are generated from whatever numeric/date/ID columns exist: totals,
  averages, unique counts, top category share, date range, growth rate, per-record
  average, and more.
- 10 chart families are available: Bar, Line, Pie, Comparison, Area, Scatter, Box,
  Histogram, Treemap, Heatmap. Each family auto-generates up to **10 different
  variants** (different dimensions/measures/aggregations) so you can explore every
  angle of the data.
- Every chart comes with a one-line **auto-generated insight** (e.g. leader, trend
  direction, correlation, skew).
- Filters are available for every column (category, numeric range, date range) and
  apply to the KPIs and all charts at once.
- Click **⭐ Add to Boss Dashboard** on any chart, or tick the pin under a KPI card,
  to send it to Page 3.

**🧩 Custom Builder (Page 1.5) — Power BI style**
- Build your own **KPI cards**: pick any column + a measure (Sum, Average, Count,
  Distinct Count, Min, Max, Median, Std Dev, Mode, Earliest/Latest date...) and,
  optionally, filters that apply to that card only.
- Build your own **charts**: choose the chart type, an X field, a Y field + measure,
  and an optional Color/Legend field — exactly like Power BI's field wells.
- Every card and every chart carries its **own independent filters** (Basic mode —
  quick dropdown/slider — or Advanced mode — operators like contains, >, between,
  is blank...), built from **every column** in the dataset, not just a fixed subset.
- Tick **⭐ Pin to Boss Dashboard** on any card or chart to send it to Page 3.

**⭐ Boss Dashboard (Page 3)**
- Shows only what you picked — clean and presentation-ready.
- Full style control: background color, font color, accent color, chart color
  palette, chart theme, font, legend on/off, data labels on/off, and an optional
  wallpaper image for the exported PDF.
- Any chart can be **swapped** for another variant of the same family without
  going back to Page 1.
- **Download PDF**: produces a clean, print-ready report with only the finished
  KPIs, charts and insights — no buttons, no settings panels, nothing that would
  look unprofessional in front of your boss.

**🗂 Data Table (Page 4)**
- SQL-style access to the raw data: pick which columns to `SELECT`, add a filter
  in pandas/SQL-like syntax (e.g. `Amount > 100000 and Status == 'Paid'`), sort,
  and export the exact slice you need as CSV.

**⚙️ Settings**
- Reset the default look of the Boss Dashboard, change your own password, and
  (for client accounts) create Report Viewer logins for your own team.

**Performance note:** column detection, KPI math and chart aggregation are all
done with vectorized pandas/NumPy operations, so the same tool comfortably
handles datasets from a few hundred rows up to several million rows. For very
large files, use the row-limit slider on the Data Table page and the filters on
every page to narrow down what's rendered on screen.

**Security note:** your login credentials are never stored in plain text.
        """)


elif page == "🔐 Admin Panel":
    if st.session_state.role != auth.ROLE_ADMIN:
        st.error("You don't have access to this page.")
        st.stop()

    st.title("🔐 Admin Panel")
    st.caption("Visible to admin accounts only — report-users never see this page.")

    tab_users, tab_mypw, tab_reset, tab_admin_about = st.tabs(
        ["👥 Manage Accounts", "🔑 Change My Own Password", "🗑️ Reset Workspace Data", "ℹ️ About This Panel"]
    )

    with tab_users:
        st.subheader("Existing accounts")
        users = auth.list_users()
        st.table([{"Username": u["username"], "Role": u["role"], "Data workspace": u["workspace_id"],
                    "Email": u["email"] or "—"} for u in users])

        st.divider()
        st.subheader("Create a new account")
        st.caption(
            "**Client** — a business/customer account with its own independent data workspace: they "
            "can upload data, build dashboards, everything except the Admin Panel.  \n"
            "**Viewer** — read-only, sees every page. Can look at reports but never upload/change "
            "anything. Either gets its own empty workspace, or you link it to an existing client's "
            "data below.  \n"
            "**Report Viewer** — restricted to ONLY the Boss Dashboard page (nothing else in the app "
            "even shows in their sidebar) but gets full control there: view, export PDF, manage "
            "slicers. Meant for a client's own boss/manager — clients can also create these "
            "themselves from their own Settings page, without needing you.  \n"
            "**Admin** — full control, same as your own account (use sparingly)."
        )
        with st.form("create_account"):
            nu = st.text_input("Username")
            np1 = st.text_input("Password", type="password")
            np2 = st.text_input("Confirm password", type="password")
            nu_email = st.text_input("Email (optional, but needed for this account's 'Forgot password' to work)")
            role_choice = st.radio("Role", ["Client", "Viewer", "Report Viewer", "Admin"], horizontal=True)

            client_options = auth.list_client_usernames()
            link_choice = None
            if role_choice in ("Viewer", "Report Viewer") and client_options:
                link_choice = st.selectbox(
                    "Data access for this account",
                    ["Give it its own independent (empty) data workspace"] +
                    [f"Share data with client: {c}" for c in client_options],
                )

            submitted = st.form_submit_button("Create / update account", type="primary")
            if submitted:
                if not nu.strip() or not np1:
                    st.error("Username and password cannot be empty.")
                elif np1 != np2:
                    st.error("Passwords do not match.")
                else:
                    role_map = {"Client": auth.ROLE_CLIENT, "Viewer": auth.ROLE_VIEWER,
                                "Report Viewer": auth.ROLE_REPORT_VIEWER, "Admin": auth.ROLE_ADMIN}
                    role = role_map[role_choice]
                    workspace_id = None  # defaults to the account's own username
                    if role in (auth.ROLE_VIEWER, auth.ROLE_REPORT_VIEWER) and link_choice and link_choice.startswith("Share data with client: "):
                        workspace_id = link_choice.replace("Share data with client: ", "")
                    auth.create_or_update_user(nu.strip(), np1, role, workspace_id=workspace_id, email=nu_email.strip())
                    st.success(f"Account '{nu.strip()}' saved as {role_choice}"
                               + (f", sharing data with '{workspace_id}'." if workspace_id else ", with its own data workspace."))
                    st.rerun()

        st.divider()
        st.subheader("Reset a password / delete an account")
        other_users = [u["username"] for u in users]
        if other_users:
            sel_user = st.selectbox("Account", other_users)
            c1, c2 = st.columns(2)
            with c1:
                with st.form("reset_pw_form"):
                    rp1 = st.text_input("New password", type="password", key="rp1")
                    rp2 = st.text_input("Confirm new password", type="password", key="rp2")
                    reset_submit = st.form_submit_button("Reset this account's password")
                    if reset_submit:
                        if not rp1 or rp1 != rp2:
                            st.error("Passwords are empty or don't match.")
                        else:
                            auth.change_password(sel_user, rp1)
                            st.success(f"Password reset for '{sel_user}'.")
                with st.form("edit_email_form"):
                    sel_email = next((u["email"] for u in users if u["username"] == sel_user), "")
                    re_email = st.text_input("Email (for their 'Forgot password')", value=sel_email, key="re_email")
                    if st.form_submit_button("Save email"):
                        auth.set_email(sel_user, re_email)
                        st.success(f"Email updated for '{sel_user}'.")
                        st.rerun()
            with c2:
                st.write("")
                st.write("")
                if st.button(f"🗑️ Delete '{sel_user}'", use_container_width=True):
                    try:
                        auth.delete_user(sel_user, st.session_state.username)
                        st.success(f"Deleted '{sel_user}'.")
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

    with tab_mypw:
        st.subheader("Change my own admin password")
        with st.form("change_own_pw"):
            cur_pw = st.text_input("Current password", type="password")
            new_pw1 = st.text_input("New password", type="password", key="my_new1")
            new_pw2 = st.text_input("Confirm new password", type="password", key="my_new2")
            submit = st.form_submit_button("Update my password")
            if submit:
                if not auth.verify_admin_login(st.session_state.username, cur_pw):
                    st.error("Current password is incorrect.")
                elif not new_pw1 or new_pw1 != new_pw2:
                    st.error("New passwords are empty or don't match.")
                else:
                    auth.change_password(st.session_state.username, new_pw1)
                    st.success("Password updated. Use the new password next time you log in.")

    with tab_reset:
        st.subheader("Clear a client's dataset")
        st.caption("The loaded dataset, Boss Dashboard layout, pinned KPIs and Custom Builder "
                   "content stay saved on disk permanently, across restarts, until you clear them "
                   "here yourself - nothing auto-deletes or auto-refreshes on its own.")

        ws_to_accounts = {}
        for u in users:
            ws_to_accounts.setdefault(u["workspace_id"], []).append(f"{u['username']} ({u['role']})")
        ws_ids = sorted(ws_to_accounts.keys())
        if not ws_ids:
            st.info("No client/viewer workspaces exist yet.")
        else:
            default_idx = ws_ids.index(st.session_state.view_as_workspace) if st.session_state.view_as_workspace in ws_ids else 0
            target_ws = st.selectbox(
                "Which client's workspace to reset?", ws_ids,
                index=default_idx,
                format_func=lambda w: f"{w}  —  used by: {', '.join(ws_to_accounts[w])}",
            )
            if ws.has_saved_data(target_ws):
                st.info(f"'{target_ws}' currently has data saved.")
            else:
                st.info(f"'{target_ws}' has nothing loaded right now.")
            st.warning(f"Resetting removes the data for **every account sharing workspace '{target_ws}'** "
                       f"({', '.join(ws_to_accounts[target_ws])}) - do this only when intentionally "
                       f"starting that client over with a new dataset.")
            confirm_reset = st.checkbox("Yes, I understand - clear this workspace's dataset and dashboard.", key="confirm_ws_reset")
            if st.button("🗑️ Reset this workspace now", disabled=not confirm_reset, type="primary"):
                ws.clear(target_ws)
                if effective_workspace_id() == target_ws:
                    for k in ws.PERSISTED_KEYS:
                        st.session_state[k] = [] if isinstance(st.session_state.get(k), list) else None
                    st.session_state.filters = {}
                    st.session_state.dashboard_name = "⭐ Boss Dashboard"
                st.success(f"Workspace '{target_ws}' cleared.")
                st.rerun()

    with tab_admin_about:
        st.subheader("Admin Panel — what only you can see")
        st.caption("This information is only ever shown here, inside the Admin Panel — "
                   "client, viewer and report-viewer accounts never see any mention of this page, "
                   "not even that it exists.")
        st.markdown("""
**⚙️ Settings (client/viewer-facing page)**
- The "How This Tool Works" tab that non-admin accounts see deliberately says nothing
  about this Admin Panel, the admin login link, or the `?admin=...` URL secret —
  they have no way to discover it exists from inside the app.

**🔐 Admin Panel (this page — admin accounts only)**
- **👥 Manage Accounts** — create new client / viewer / report-viewer accounts, reset
  anyone's password, delete accounts, and link a viewer to an existing client's data
  workspace.
- **🔑 Change My Own Password** — update your own admin login.
- **🗑️ Reset Workspace Data** — wipe a client's loaded dataset/dashboard so they
  can start over with fresh data.
- Client, viewer and report-viewer accounts can never see or reach this page — it
  isn't in their sidebar at all, and their normal login has no path to it. The
  hidden admin login form only appears when this app is opened with the secret
  `?admin=...` URL value set in `app.py` (`ADMIN_URL_SECRET`).

**Security note:** credentials are stored as SHA-256 hashes in `credentials.json`
next to this app — never in plain text. Only accounts with the "admin" role can
create/delete users or reset passwords; client, viewer and report-viewer accounts
have no access to any credential screen, and never see this tab.
        """)


# ==================================================================================
# AUTO-SAVE WORKSPACE TO DISK
# ==================================================================================
# Runs at the end of every script run (Streamlit reruns this whole file on every
# click/upload/etc.), so whatever is in session_state right now is always what's
# on disk too. This is the only thing that makes data survive an app.py restart -
# it is intentionally NOT tied to any particular button or page.
if st.session_state.authenticated and st.session_state.df_raw is not None:
    ws.save(st.session_state, effective_workspace_id())


# footer
st.markdown(
    """
    <style>
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)
