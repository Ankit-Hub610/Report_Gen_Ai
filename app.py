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
import datetime
import numpy as np
import pandas as pd
import streamlit as st
import extra_streamlit_components as stx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from modules import auth, data_engine as de, chart_engine as ce, pdf_export as pe
from modules import measures as ms, builder_engine as be
from modules import workspace_store as ws
from modules import query_engine as qe
from modules import db_connector as dbc
from modules import ai_chat as ac
from modules import email_service as es
from modules import intel_engine as ie
from modules import ppt_engine as ppt
from modules import voice_engine as ve
from modules import usage_limits as ul
from modules import payments as pay

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_AVAILABLE = True
except ImportError:
    AUTOREFRESH_AVAILABLE = False

try:
    from streamlit_mic_recorder import speech_to_text
    MIC_AVAILABLE = True
except ImportError:
    MIC_AVAILABLE = False

# ==================================================================================
# PAGE CONFIG
# ==================================================================================
st.set_page_config(page_title="RA-I Created by Ankit_Solanki", page_icon="🏆", layout="wide")

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(APP_DIR, "sample_data", "sample_sports_payments.csv")  # kept for backward-compat
SAMPLE_DATASETS = {
    "🏸 Sports / court bookings & payments": "sample_sports_payments.csv",
    "🛍️ E-commerce / D2C sales": "sample_ecommerce_sales.csv",
    "🧾 Freelancer / agency invoices": "sample_agency_invoices.csv",
}

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

DEFAULT_BRAND = {
    "text": "🏆 RA-Intelligence",
    "font_size": 22,       # px, sidebar heading
    "color": "#F5F5F5",
    "bold": True,
    "italic": False,
    "font_family": "sans-serif",  # sans-serif / serif / monospace
    # Login-page logo, one per theme mode (dark visitor / light visitor).
    # Each is raw image bytes (PNG/JPG) ready to hand straight to st.image -
    # a PDF upload gets its first page rendered down to PNG bytes before
    # being stored here (see _process_logo_file).
    "logo_dark": None,
    "logo_dark_mime": None,
    "logo_light": None,
    "logo_light_mime": None,
    "logo_width": 220,     # px — explicit size control for the login-page logo
    # ---- Neon / glow lighting (advanced) --------------------------------------
    "glow_enabled": False,
    "glow_targets": ["text", "logo"],  # subset of "text" (sidebar brand text) / "logo" (login logo)
    "glow_color": "#00E5FF",           # aqua/cyan — the classic neon look, but fully customizable
    "glow_style": "pulse",             # "steady" | "pulse" | "flicker" | "rainbow"
    "glow_intensity": 16,              # px — base glow size (bigger = thicker halo)
    "glow_speed": 2.2,                 # seconds per animation cycle (ignored by "steady")
}

FAMILY_ICONS = {
    "Bar": "📊", "Line": "📈", "Pie": "🥧", "Comparison": "⚖️", "Area": "🏔️",
    "Scatter": "🔵", "Box": "📦", "Histogram": "📶", "Treemap": "🌳", "Heatmap": "🔥",
}


# ==================================================================================
# SESSION STATE INIT
# ==================================================================================
def init_state():
    ss = st.session_state
    ss.setdefault("authenticated", False)
    ss.setdefault("username", None)
    ss.setdefault("role", None)
    ss.setdefault("plan", "standard")   # "standard" (unlimited) or "free" (capped, see usage_limits.py)
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
    ss.setdefault("p1_kpi_format", {})      # {kpi_label: format_code} — per-card NUMBER FORMAT (e.g. one card
                                             # shows "58.18 L", another shows "5,818,432.00", another "12.3%") —
                                             # each card remembers its own choice independently of every other card.
    ss.setdefault("p1_kpi_custom_code", {})  # {kpi_label: "custom Excel-style code"} — only used when that
                                              # card's format is set to "Custom (type Excel format code)"
    ss.setdefault("page", "Connect Data")
    ss.setdefault("data_source_name", None)
    ss.setdefault("custom_kpis", [])        # list of user-built KPI card dicts (Custom Builder)
    ss.setdefault("custom_charts", [])      # list of user-built chart dicts (Custom Builder, Power-BI style)
    ss.setdefault("dashboard_slicers", [])  # list of dicts: {field, style} - Boss Dashboard slicer widgets
    ss.setdefault("dashboard_zoom", {})     # {chart_key: [start_pct, end_pct]} - persisted per-chart zoom window,
                                             # applied identically on-screen AND when the SAME chart is exported to PDF
    ss.setdefault("dashboard_name", "⭐ Boss Dashboard")  # fully editable Boss Dashboard title
    # App branding (sidebar title) - GLOBAL across every account, admin-editable.
    # Loaded from disk once per session (not per-workspace - see workspace_store.load_branding).
    if "app_brand" not in ss:
        saved_brand = ws.load_branding()
        ss["app_brand"] = {**DEFAULT_BRAND, **saved_brand} if saved_brand else copy.deepcopy(DEFAULT_BRAND)
    # External Database Connector (Data Table page) - NEVER persisted to disk (see workspace_store.py)
    ss.setdefault("ai_chat_history", [])     # list of {role, content, ts} — the AI Assistant page's chat log
    ss.setdefault("_chat_history_loaded_ws", None)  # which workspace's saved chat history is currently loaded into ai_chat_history
    ss.setdefault("ai_groq_key", None)       # session-only OpenRouter API key typed into the UI by admin (never written to disk)
    ss.setdefault("db_conn_uri", "")
    ss.setdefault("db_conn_type", "PostgreSQL")
    ss.setdefault("db_connected", False)
    ss.setdefault("data_source_is_db", False)   # was the CURRENT df_raw loaded from a database (vs file upload)?
    ss.setdefault("db_last_load_sql", "")        # exact query used, so "Refresh from database" can re-run it
    ss.setdefault("db_last_load_label", "")      # table/description shown in the "connected live" banner
    ss.setdefault("db_last_refreshed_at", None)  # time.time() of the last successful (re)load, for the banner
    ss.setdefault("db_auto_sync_seconds", 0)     # 0 = off; >0 = auto-refresh interval selected on Connect Data page
    ss.setdefault("db_queries", [])         # list of query-tab dicts, see modules/db_connector.py
    ss.setdefault("db_query_results", {})   # {query_id: DataFrame}

    # 📈 Full Analysis page
    ss.setdefault("intel_role_overrides", {})   # user-confirmed column-role mapping (persisted)
    ss.setdefault("intel_language", "English")  # persisted — last-picked narrative language
    ss.setdefault("assistant_name", None)   # None = show "री" (see voice_engine.get_assistant_name)
    ss.setdefault("voice_language", "English")  # persisted — last-picked voice assistant language
    ss.setdefault("intel_action_checks", [])    # persisted — [{text, done}] Top-Actions tracker
    ss.setdefault("intel_part", 1)              # session-only — which half of the report is showing (1 or 2)
    ss.setdefault("_intel_cache_key", None)     # session-only — fingerprint of last-computed facts
    ss.setdefault("_intel_facts", None)         # session-only — cached facts bundle (dict of real numbers)
    ss.setdefault("_intel_narrative", None)     # session-only — cached AI narrative text, keyed by (cache_key, language)
    ss.setdefault("intel_qa_history", [])       # session-only — follow-up Q&A chat log on this page


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


def _render_chart_with_zoom(fig, zoom_key: str, widget_key: str, editable: bool = True):
    """Renders a Boss Dashboard chart with a compact '🔍 Zoom' range slider above
    it. The chosen zoom window (0-100%) is stored in st.session_state.dashboard_zoom
    (persisted), applied to THIS SAME fig object before st.plotly_chart draws it -
    and the caller then reuses that same fig for the PDF export, so whatever zoom
    is showing on screen is exactly what appears in the exported PDF too. Returns
    the (possibly zoom-applied) fig so the caller can hand it to chart_png_items."""
    zoom = st.session_state.dashboard_zoom.get(zoom_key, [0, 100])
    if editable:
        zc1, zc2 = st.columns([1, 20])
        with zc1:
            st.markdown("🔍")
        with zc2:
            new_zoom = st.slider("Zoom (show data range %)", 0, 100, tuple(zoom), step=1,
                                  key=f"zoom_{widget_key}", label_visibility="collapsed",
                                  help="Drag either handle to zoom in on crowded data labels. "
                                       "This exact zoom is what will appear in the exported PDF too.")
            new_zoom = list(new_zoom)
            if new_zoom != zoom:
                st.session_state.dashboard_zoom[zoom_key] = new_zoom
                ws.save_light(st.session_state, st.session_state.workspace_id)
                zoom = new_zoom
    if zoom != [0, 100]:
        fig = ce.apply_zoom_window(fig, zoom[0], zoom[1])
    st.plotly_chart(fig, use_container_width=True, key=widget_key, config=ce.PLOTLY_CONFIG)
    return fig


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
        # this file, which an immediate rerun never reaches). Without handling
        # that, the very next run's reload below would load a stale pre-change
        # copy from disk and silently undo whatever was just clicked.
        #
        # FIX (this used to be the actual bug behind "Refresh does nothing"):
        # the old code ALWAYS saved this session's current light state to disk
        # before reading it back. That's correct when THIS session made the
        # latest change (nothing lost) - but when a DIFFERENT session (e.g. a
        # linked report-viewer clicking Refresh, having made no local edits)
        # ran this, it kept re-saving its own STALE copy over whatever the
        # other session had just saved, THEN read that same stale copy back -
        # so Refresh silently clobbered the other session's newer change
        # instead of picking it up.
        #
        # Correct behaviour: only flush-save if THIS session actually has an
        # unsaved local change (its current light state differs from what we
        # last synced to/from disk). Otherwise, just load - never overwrite
        # with a copy we know is unchanged from what we already had.
        #
        # PERF: this only ever needs to sync dashboard config (charts, pinned
        # KPIs, slicers, name, pivot reports) between sessions sharing a
        # workspace - never the dataset itself (df_raw only changes when
        # THIS session explicitly loads new data, which already updates its
        # own session_state directly). So this uses save_light()/load_light()
        # instead of the full save()/load(), which used to re-pickle the
        # WHOLE dataset on every single pin/unpin click - that was the cause
        # of the reported lag on this page.
        #
        # NOTE on deepcopy below: some actions (e.g. removing a pinned KPI
        # card) mutate a LIGHT_KEYS list/dict IN PLACE (list.remove(...),
        # card["pinned"] = False) instead of assigning a new object. If the
        # snapshot only stored the same object reference, that in-place edit
        # would silently show up in the snapshot too - making current_light
        # look unchanged from last_synced, so the real edit would never get
        # flushed to disk (and the very next load would overwrite it back to
        # the old, pre-removal state). Deep-copying the snapshot is what
        # makes it a true "what disk had last", independent of later in-place
        # edits to session_state.
        current_light = {k: ss.get(k) for k in ws.LIGHT_KEYS}
        last_synced = ss.get("_light_synced_snapshot")
        if ss.get("df_raw") is not None and current_light != last_synced:
            ws.save_light(ss, wsid)
            ss["_light_synced_snapshot"] = copy.deepcopy(current_light)
        else:
            light = ws.load_light(wsid)
            for k, v in light.items():
                if v is not None:
                    ss[k] = v
            ss["_light_synced_snapshot"] = copy.deepcopy({k: ss.get(k) for k in ws.LIGHT_KEYS})

    if workspace_changed:
        # Rare event (login, or admin switching "View as") - the full
        # dataset genuinely needs to load here, so use the full load().
        saved = ws.load(wsid)
        if saved:
            for k, v in saved.items():
                if v is not None:
                    ss[k] = v
        # What we just loaded from disk is already in sync with disk - mark it
        # as such so the end-of-script auto-save doesn't immediately re-save
        # the whole dataset again this same run.
        ss["_last_saved_df_id"] = id(ss.get("df_raw"))
        ss["_light_synced_snapshot"] = copy.deepcopy({k: ss.get(k) for k in ws.LIGHT_KEYS})
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
# refresh/reopen, but it is never part of the URL text, so copy-pasting the
# link into another browser/device carries no credential at all — that
# browser has to log in for real. Opening a second TAB in the *same*
# browser will still be logged in, same as any normal website (Gmail,
# etc.) — that's expected, not a leak, since it's still the same physical
# browser holding the cookie.
#
# Why extra_streamlit_components.CookieManager and not a hand-rolled
# components.html(<script>document.cookie=...) trick: that was tried first
# and was unreliable — components.html() renders inside a nested iframe,
# and Streamlit's own re-render cycle could tear that iframe down before
# its script had actually run, so the cookie write sometimes silently never
# happened (login, refresh, bounced back to the login screen). CookieManager
# is a real bidirectional Streamlit component (like any other widget) that
# Streamlit itself guarantees finishes mounting and reports its value back
# before treating the run as complete, so there's no race to lose.
SESSION_COOKIE_NAME = "app_session"


def _get_cookie_manager():
    return stx.CookieManager(key="app_cookie_manager")


# One instance, created once per script run, used by every login/logout
# path below and by the refresh-restore check further down this file.
cookie_manager = _get_cookie_manager()


def _set_session_cookie(token: str):
    """Sets the 'stay logged in' cookie in the browser. Caller is
    responsible for calling st.rerun() right after this (CookieManager
    handles its own component lifecycle - no manual page reload needed)."""
    expires_at = datetime.datetime.now() + datetime.timedelta(seconds=auth.SESSION_LIFETIME_SECONDS)
    cookie_manager.set(SESSION_COOKIE_NAME, token, expires_at=expires_at, key="set_session_cookie")


def _clear_session_cookie():
    """Clears the 'stay logged in' cookie. Caller is responsible for calling
    st.rerun() right after this."""
    try:
        cookie_manager.delete(SESSION_COOKIE_NAME, key="del_session_cookie")
    except KeyError:
        pass  # already gone (e.g. cookie had already expired) - nothing to clear


def _get_session_cookie():
    try:
        return cookie_manager.get(cookie=SESSION_COOKIE_NAME)
    except Exception:
        return None  # cookie component hasn't finished its first mount yet - fails safe (just asks to log in again)


def _detect_theme_mode():
    """Best-effort 'dark' or 'light' for the CURRENT visitor, so the right
    login logo can be shown. Tries the real per-visitor theme first
    (Streamlit 1.38+ auto-detects the browser/OS preference under 'auto'),
    falls back to the app-wide config default, and finally just 'dark'."""
    try:
        return st.context.theme.type  # 'light' or 'dark' — reflects THIS visitor's actual rendered theme
    except Exception:
        pass
    try:
        base = st.get_option("theme.base")
        if base in ("light", "dark"):
            return base
    except Exception:
        pass
    return "dark"


def _process_logo_file(uploaded_file):
    """Turns an uploaded PNG/JPG/JPEG/PDF into (bytes, mime) ready to store
    and hand straight to st.image. PDFs get their first page rendered down
    to a PNG (needs the optional PyMuPDF package - see requirements.txt).
    Returns (None, None) and shows an st.error on failure."""
    raw = _read_upload(uploaded_file)
    name = uploaded_file.name.lower()
    if name.endswith(".pdf"):
        try:
            import fitz  # PyMuPDF
        except ImportError:
            st.error("PDF logos need the optional **PyMuPDF** package — add `PyMuPDF` to "
                      "requirements.txt and redeploy, or upload a PNG/JPG instead.")
            return None, None
        try:
            doc = fitz.open(stream=raw, filetype="pdf")
            pix = doc.load_page(0).get_pixmap(matrix=fitz.Matrix(3, 3))  # ~3x zoom, crisp small logo
            return pix.tobytes("png"), "image/png"
        except Exception as e:
            st.error(f"Couldn't read that PDF: {e}")
            return None, None
    mime = "image/jpeg" if name.endswith((".jpg", ".jpeg")) else "image/png"
    return raw, mime


def _glow_css(css_class: str, kind: str, color: str, style: str, intensity: int, speed: float) -> str:
    """Builds a <style> block that gives .{css_class} a neon/glow lighting
    effect. kind is "text" (uses text-shadow, hugs the letter outlines) or
    "logo" (uses filter: drop-shadow, which hugs the *visible pixels* of the
    image rather than its rectangular bounding box — so a logo PNG with a
    transparent background gets a glow that follows the actual letter/icon
    shape instead of lighting up a box behind it).

    style options:
      "steady"  — constant glow, no animation. Classic always-on neon sign.
      "pulse"   — smoothly breathes bigger/smaller (the most common neon look).
      "flicker" — irregular on/off flicker, like an old/cheap neon tube.
      "rainbow" — the glow colour itself cycles through the spectrum
                  (ignores `color` — that's the whole point of this one).
    """
    i1, i2, i3, i4 = intensity, intensity * 2, intensity * 3, intensity * 4

    def decl(a, b):
        if kind == "logo":
            return f"filter: drop-shadow(0 0 {a}px {color}) drop-shadow(0 0 {b}px {color});"
        return f"text-shadow: 0 0 {a}px {color}, 0 0 {b}px {color};"

    def decl_off():
        return "filter: none;" if kind == "logo" else "text-shadow: none;"

    if style == "steady":
        return f"<style>.{css_class} {{ {decl(i1, i2)} }}</style>"

    if style == "pulse":
        return f"""<style>
        @keyframes {css_class}_kf {{
            0%, 100% {{ {decl(i1 * 0.5, i1)} }}
            50%      {{ {decl(i3, i4)} }}
        }}
        .{css_class} {{ animation: {css_class}_kf {speed}s ease-in-out infinite; }}
        </style>"""

    if style == "flicker":
        # Uneven timing on purpose — a real neon tube flicker isn't a smooth sine wave.
        return f"""<style>
        @keyframes {css_class}_kf {{
            0%, 18%, 22%, 25%, 53%, 57%, 100% {{ {decl(i2, i3)} opacity: 1; }}
            20%, 24%, 55% {{ {decl_off()} opacity: 0.4; }}
        }}
        .{css_class} {{ animation: {css_class}_kf {speed * 2.5}s linear infinite; }}
        </style>"""

    if style == "rainbow":
        return f"""<style>
        @keyframes {css_class}_kf {{
            0%   {{ filter: hue-rotate(0deg) drop-shadow(0 0 {i2}px {color}); }}
            100% {{ filter: hue-rotate(360deg) drop-shadow(0 0 {i2}px {color}); }}
        }}
        .{css_class} {{ animation: {css_class}_kf {speed * 3}s linear infinite; }}
        </style>"""

    return ""


def _render_glow_target(css_class: str, kind: str, brand: dict, inner_html: str):
    """Wraps `inner_html` in the glow class + injects its <style> block, but
    ONLY if glow is turned on and this kind ("text"/"logo") is one of the
    selected targets — otherwise just renders inner_html untouched."""
    if brand.get("glow_enabled") and kind in (brand.get("glow_targets") or []):
        st.markdown(_glow_css(css_class, kind, brand["glow_color"], brand["glow_style"],
                               brand["glow_intensity"], brand["glow_speed"]), unsafe_allow_html=True)
        st.markdown(f'<div class="{css_class}">{inner_html}</div>', unsafe_allow_html=True)
    else:
        st.markdown(inner_html, unsafe_allow_html=True)


def _logo_img_html(logo_bytes: bytes, mime: str, width: int, extra_style: str = "") -> str:
    import base64
    b64 = base64.b64encode(logo_bytes).decode()
    return (f'<img src="data:{mime};base64,{b64}" style="width:{width}px; max-width:100%; '
            f'display:block; margin:0 auto; border-radius:10px; {extra_style}">')
    """Downloads a direct image link (e.g. right-click an image -> 'Copy image
    address', including Google-hosted image URLs) and returns (bytes, mime).
    Returns (None, None) and shows an st.error if the link doesn't point at
    an actual image."""
    import requests
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        ctype = resp.headers.get("Content-Type", "")
        if not ctype.startswith("image/"):
            st.error("That link doesn't point directly at an image file — right-click the image "
                      "itself (not the page it's on) and choose 'Copy image address'.")
            return None, None
        return resp.content, ctype.split(";")[0].strip()
    except Exception as e:
        st.error(f"Couldn't fetch that image: {e}")
        return None, None


def login_screen():
    brand = st.session_state.get("app_brand") or DEFAULT_BRAND
    _mode = _detect_theme_mode()
    _logo = brand.get(f"logo_{_mode}") or brand.get("logo_dark") or brand.get("logo_light")
    _logo_mime = brand.get(f"logo_{_mode}_mime") or brand.get("logo_dark_mime") or brand.get("logo_light_mime") or "image/png"
    if _logo:
        _lc1, _lc2, _lc3 = st.columns([1, 1, 1])
        with _lc2:
            _img_html = _logo_img_html(_logo, _logo_mime, brand.get("logo_width", 220))
            _render_glow_target("brand_glow_logo", "logo", brand, _img_html)
    st.markdown("<h1 style='text-align:center;'>Research | Analysis | Intellegance </h1>", unsafe_allow_html=True)
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
                    st.session_state.plan = auth.get_effective_plan(u.strip())
                    _token = auth.create_session(u.strip())
                    st.session_state._session_token = _token
                    _set_session_cookie(_token)  # survives a browser refresh, but not copy-paste into another browser
                    st.rerun()
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
                        st.rerun()
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
# hitting F5/reload. Before falling back to the login screen, check the cookie
# CookieManager keeps across that reload — if it's still valid, log the person
# back in silently instead of making them type their password again.
#
# IMPORTANT: on a brand-new session (i.e. right after a real F5 refresh),
# CookieManager hasn't actually heard back from the browser yet on THIS run —
# reading a cookie is a one-way trip out to the browser and back, and that
# reply doesn't arrive until the NEXT rerun. get_all() returns None to mean
# specifically "haven't heard back yet" (as opposed to {} = "heard back, no
# cookies"). Treating that None the same as "not logged in" (which the
# earlier version of this code did) is exactly what was still sending people
# to the login screen on every refresh even with a valid cookie sitting
# right there in the browser. So: if it's still None, stop and wait for the
# automatic rerun CookieManager triggers once it has the real answer -
# don't conclude "not logged in" from an answer that hasn't arrived yet.
if not st.session_state.authenticated:
    _all_cookies = cookie_manager.get_all()
    if _all_cookies is None:
        st.info("Restoring your session…")
        st.stop()
    _session_token = _all_cookies.get(SESSION_COOKIE_NAME)
    _resolved_user = auth.resolve_session(_session_token) if _session_token else None
    if _resolved_user and auth.user_exists(_resolved_user):
        st.session_state.authenticated = True
        st.session_state.username = _resolved_user
        st.session_state.role = auth.get_role(_resolved_user)
        st.session_state.workspace_id = auth.get_workspace_id(_resolved_user)
        st.session_state.plan = auth.get_effective_plan(_resolved_user)
        st.session_state._session_token = _session_token

if not st.session_state.authenticated:
    login_screen()
    st.stop()

# Always recompute the CURRENT plan/subscription status from disk on every
# rerun (credentials.json is tiny - this is a cheap read), instead of only
# trusting whatever was cached in session_state at login/cookie-restore
# time. This is what makes an admin's approval (or a subscription's natural
# expiry) show up the moment the client next clicks/navigates ANYTHING -
# no more needing to log out and log back in just to see it. The 💎 Plans
# page below additionally has its own short auto-refresh timer, for the
# case where someone is just sitting there staring at the page waiting.
st.session_state.plan = auth.get_effective_plan(st.session_state.username)


def render_plan_comparison():
    """Free vs Standard comparison table — shown both as its own nav page for
    logged-in users, and inside the trial-expired blocking screen below."""
    st.markdown("### 🆓 Free  vs  💎 Standard")
    rows = [
        ("AI requests", f"{ul.FREE_PLAN_LIMITS['ai_calls']} per day", "Unlimited"),
        ("PDF exports", f"{ul.FREE_PLAN_LIMITS['pdf_exports']} per day, watermarked", "Unlimited, no watermark"),
        ("Data size", f"Up to {ul.FREE_PLAN_LIMITS['max_rows']:,} rows", "Unlimited rows"),
        ("How long it works", f"{auth.TRIAL_DAYS} days total, then access stops", "Forever"),
        ("Boss Dashboard, Custom Builder, Full Analysis", "✅ Included", "✅ Included"),
        ("Branding / white-label", "✅ Included", "✅ Included"),
        ("Priority support", "—", "✅ Included"),
    ]
    df_plan = pd.DataFrame(rows, columns=["Feature", "🆓 Free", "💎 Standard"])
    st.table(df_plan.set_index("Feature"))
    st.caption(f"Free plan is meant for trying the tool out — full access for {auth.TRIAL_DAYS} days, "
               f"with generous-but-capped daily usage. After that, upgrade to Standard to keep going "
               f"with everything unlocked and unlimited.")


def render_pricing_cards(key_prefix: str = "") -> str:
    """Two side-by-side pricing cards (Monthly / Yearly), styled like a real
    pricing page — not a plain radio button list. Returns the plan_type
    ('monthly' or 'yearly') the person has selected. Selection is kept in
    session_state so it survives the rerun a card click triggers."""
    pay_cfg = pay.get_config()
    state_key = f"{key_prefix}selected_plan_type"
    st.session_state.setdefault(state_key, pay.PLAN_MONTHLY)

    yearly_monthly_equiv = pay_cfg["yearly_price"] / 12
    savings_pct = 0
    if pay_cfg["monthly_price"] > 0:
        savings_pct = round(100 * (1 - yearly_monthly_equiv / pay_cfg["monthly_price"]))

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            picked = st.session_state[state_key] == pay.PLAN_MONTHLY
            st.markdown(f"#### 📅 Monthly {'✅' if picked else ''}")
            st.markdown(f"## ₹{pay_cfg['monthly_price']:.0f}")
            st.caption("per month, cancel anytime")
            if st.button("Choose Monthly", key=f"{key_prefix}pick_monthly",
                        type="primary" if picked else "secondary", use_container_width=True):
                st.session_state[state_key] = pay.PLAN_MONTHLY
                st.rerun()
    with c2:
        with st.container(border=True):
            picked = st.session_state[state_key] == pay.PLAN_YEARLY
            badge = f" 🏷️ Save {savings_pct}%" if savings_pct > 0 else ""
            st.markdown(f"#### 🗓️ Yearly {'✅' if picked else ''}{badge}")
            st.markdown(f"## ₹{pay_cfg['yearly_price']:.0f}")
            st.caption(f"per year (≈ ₹{yearly_monthly_equiv:.0f}/month)")
            if st.button("Choose Yearly", key=f"{key_prefix}pick_yearly",
                        type="primary" if picked else "secondary", use_container_width=True):
                st.session_state[state_key] = pay.PLAN_YEARLY
                st.rerun()
    return st.session_state[state_key]


def render_upi_upgrade_flow(plan_type: str, key_prefix: str = ""):
    """The actual 'pay and request upgrade' UI for ONE selected plan
    (monthly or yearly) — a real (admin-verified, not auto-trust) manual UPI
    flow, since there's no payment gateway wired up yet."""
    pay_cfg = pay.get_config()
    if not pay_cfg["upi_id"]:
        st.markdown("**Want to upgrade to Standard?** Contact your admin to arrange payment.")
        return
    amount = pay_cfg["yearly_price"] if plan_type == pay.PLAN_YEARLY else pay_cfg["monthly_price"]
    st.caption("Pay via any UPI app (GPay, PhonePe, Paytm, etc.), then submit your transaction "
              "reference below. An admin verifies and activates your account — usually within "
              "a few hours, not instant (this keeps the process honest without a payment gateway yet).")
    upi_col1, upi_col2 = st.columns([1, 1])
    upi_link = pay.build_upi_link(amount, f"Standard ({plan_type}) - {st.session_state.username}")
    with upi_col1:
        st.metric(f"{plan_type.title()} price", f"₹{amount:.0f}")
        st.markdown(f"**Pay to UPI ID:** `{pay_cfg['upi_id']}`")
        if pay_cfg.get("payee_name"):
            st.caption(f"Payee name: {pay_cfg['payee_name']}")
        st.link_button("📲 Open in UPI app (on phone)", upi_link, use_container_width=True)
    with upi_col2:
        qr_bytes = pay.qr_png_bytes(upi_link)
        if qr_bytes:
            st.image(qr_bytes, caption="Scan with any UPI app", width=180)

    st.divider()
    with st.form(f"{key_prefix}upi_submit_form_{plan_type}"):
        utr_input = st.text_input("UPI transaction reference / UTR number",
                                  help="Shown in your UPI app right after payment succeeds "
                                       "(e.g. GPay: tap the payment → 'UPI transaction ID').")
        submitted_utr = st.form_submit_button("Submit for verification", type="primary")
    if submitted_utr:
        ok_utr, msg_utr = pay.submit_request(st.session_state.username, st.session_state.workspace_id,
                                             utr_input, amount, plan_type=plan_type)
        (st.success if ok_utr else st.error)(msg_utr)

    my_reqs = [r for r in pay.list_requests() if r["username"] == st.session_state.username]
    if my_reqs:
        st.caption("Your submitted requests:")
        st.dataframe(pd.DataFrame([
            {"Submitted": datetime.datetime.fromtimestamp(r["submitted_at"]).strftime("%d %b %Y %H:%M"),
             "Plan": r.get("plan_type", "monthly").title(), "UTR": r["utr"], "Amount": f"₹{r['amount']:.0f}",
             "Status": r["status"].title()}
            for r in my_reqs
        ]), use_container_width=True, hide_index=True)


@st.dialog("💎 Upgrade to Standard")
def upgrade_dialog():
    """The popup — this is what makes upgrading feel like a real product's
    pricing modal (Notion/Slack-style) instead of a plain page section.
    Triggered by an 'Upgrade' button; everything happens inside this modal
    without leaving whatever page the person was on."""
    chosen = render_pricing_cards(key_prefix="dlg_")
    st.divider()
    render_upi_upgrade_flow(chosen, key_prefix="dlg_")
    if st.button("Close", key="dlg_close_upgrade"):
        st.rerun()


if st.session_state.plan == "free" and st.session_state.role != auth.ROLE_ADMIN:
    _trial = auth.get_trial_status(st.session_state.username)
    if _trial["expired"]:
        st.title("⏳ Your free trial has ended")
        st.warning(f"Your {auth.TRIAL_DAYS}-day free trial finished. Upgrade to Standard to keep using "
                   f"the tool — your data is safe and waiting, it isn't deleted.")
        render_plan_comparison()
        st.divider()
        st.subheader("⬆️ Upgrade to Standard")
        _chosen_plan = render_pricing_cards(key_prefix="trial_expired_")
        st.divider()
        render_upi_upgrade_flow(_chosen_plan, key_prefix="trial_expired_")
        if st.button("Log out"):
            auth.destroy_session(st.session_state.get("_session_token"))
            st.session_state.authenticated = False
            st.session_state.username = None
            st.session_state.role = None
            st.session_state.plan = "standard"
            st.session_state.workspace_id = None
            st.session_state._session_token = None
            _clear_session_cookie()
            st.rerun()
        st.stop()

if st.session_state.plan == "expired_standard" and st.session_state.role != auth.ROLE_ADMIN:
    st.title("🔁 Your subscription has ended")
    _sub = auth.get_subscription_status(st.session_state.username)
    _cycle = (_sub.get("billing_cycle") or "monthly").title()
    st.warning(f"Your {_cycle} Standard subscription has ended — renew to keep everything unlocked. "
               f"Your data is safe and waiting, nothing has been deleted.")
    render_plan_comparison()
    st.divider()
    st.subheader("🔁 Renew your subscription")
    _chosen_plan_renew = render_pricing_cards(key_prefix="sub_expired_")
    st.divider()
    render_upi_upgrade_flow(_chosen_plan_renew, key_prefix="sub_expired_")
    if st.button("Log out", key="logout_sub_expired"):
        auth.destroy_session(st.session_state.get("_session_token"))
        st.session_state.authenticated = False
        st.session_state.username = None
        st.session_state.role = None
        st.session_state.plan = "standard"
        st.session_state.workspace_id = None
        st.session_state._session_token = None
        _clear_session_cookie()
        st.rerun()
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
    df, cap_note = _enforce_row_cap(df)
    st.session_state.df_raw = df
    st.session_state.meta = de.profile_columns(df)
    st.session_state.data_source_name = source_name + (f" {cap_note}" if cap_note else "")
    st.session_state.filters = {}
    st.session_state.dashboard_charts = []
    st.session_state.pinned_kpis = []
    st.session_state.dashboard_slicers = []
    st.session_state.data_source_is_db = False   # a fresh file/sample load — any previous DB link no longer applies
    if cap_note:
        st.warning(f"🆓 Free plan is capped at {ul.FREE_PLAN_LIMITS['max_rows']:,} rows — "
                   f"loaded the first {ul.FREE_PLAN_LIMITS['max_rows']:,} rows of this file. "
                   f"Ask your admin to upgrade your plan for full data.")


def _enforce_row_cap(df):
    """Truncates df to the free-plan row cap, if this account is on 'free'.
    Returns (possibly-truncated df, short note-or-None to append to the
    displayed source name so it's obvious the data was capped)."""
    cap = ul.row_limit_for_plan(st.session_state.get("plan", "standard"))
    if cap is not None and len(df) > cap:
        return df.head(cap).copy(), f"(capped to {cap:,} rows — free plan)"
    return df, None


def _refresh_loaded_df(df, source_name):
    """Like _apply_loaded_df, but for an in-place 'get the latest data' refresh
    of an ALREADY-loaded database source: updates the data itself but keeps
    whatever charts/KPI cards/slicers/filters were already built on top of it
    (a refresh should feel like 'this table just got new rows', not 'start
    the dashboard over from scratch'). Used by the manual Refresh button and
    by auto-sync."""
    df, cap_note = _enforce_row_cap(df)
    st.session_state.df_raw = df
    st.session_state.meta = de.profile_columns(df)
    st.session_state.data_source_name = source_name + (f" {cap_note}" if cap_note else "")
    st.session_state.db_last_refreshed_at = time.time()


def _run_db_refresh():
    """Re-runs the last-used database query and applies the result. Returns
    (ok, message). Safe to call from a button OR from an auto-sync rerun."""
    if not st.session_state.db_conn_uri or not st.session_state.db_last_load_sql:
        return False, ("No live database connection in this browser session — go to "
                        "**📥 Connect Data → Connect Database** and load a table again "
                        "(the connection isn't kept between browser sessions, for security: "
                        "it would mean storing your database password on disk).")
    try:
        result_df = dbc.run_query(st.session_state.db_conn_uri, st.session_state.db_last_load_sql)
        cleaned = de.clean_dataframe(result_df)
        _refresh_loaded_df(cleaned, st.session_state.data_source_name)
        return True, f"Refreshed — {len(cleaned):,} rows as of now."
    except dbc.QueryError as e:
        return False, f"Refresh failed: {e}"


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


def load_sample(filename: str = "sample_sports_payments.csv"):
    """Loads one of the built-in demo datasets (see SAMPLE_DATASETS above) so a
    brand-new user can see the whole tool working in one click, before trusting
    it with their own data. Offering more than one industry's worth of sample
    data (not just sports) makes the "this works on ANY data" claim obvious
    immediately, instead of the user having to take our word for it."""
    sample_path = os.path.join(APP_DIR, "sample_data", filename)
    if not os.path.exists(sample_path):
        st.error("Sample file not found.")
        return
    with open(sample_path, "rb") as f:
        data = _read_upload(f)
    sheets = _load_and_clean(data, filename)
    df = sheets["Sheet1"]
    st.session_state.df_raw = df
    st.session_state.meta = de.profile_columns(df)
    st.session_state.data_source_name = f"{filename} (demo data)"
    st.session_state.filters = {}
    st.session_state.dashboard_charts = []
    st.session_state.pinned_kpis = []
    st.session_state.dashboard_slicers = []


def render_voice_assistant(page_key: str, walkthrough_segments: list,
                            df=None, meta=None, kpis=None, dashboard_charts=None):
    """The 🎤 voice assistant block — used on Full Analysis, Business
    Insights, and Boss Dashboard. Same three pieces everywhere (see
    modules/voice_engine.py docstring for the full reasoning):
      1. Push-to-talk mic button -> browser speech-to-text (free, no key)
      2. Question answered by the SAME engine as the 🤖 AI Assistant page
         (real SQL against the real data, not a guess)
      3. Answer spoken aloud via the browser's own text-to-speech (free, no key)
    Plus a Guided Walkthrough that steps through `walkthrough_segments`
    (built from data already computed on the calling page - never invented
    here) with Play/Next/Previous, each step spoken aloud too.
    """
    assistant_name = ve.get_assistant_name(st.session_state.plan, st.session_state.get("assistant_name"))
    lang_label = st.session_state.get("voice_language", "English")
    if lang_label not in ve.LANG_CODES:
        # Guards against a stale value left over from an older deploy (e.g. a
        # language option that used to exist and no longer does) crashing the
        # whole page with a ValueError on .index() below.
        lang_label = "English"
        st.session_state.voice_language = lang_label

    with st.expander(f"🎤 {assistant_name} — poochiye ya guided walkthrough suniye", expanded=False):
        if not MIC_AVAILABLE:
            st.caption("⚠️ Voice needs the `streamlit-mic-recorder` package — not installed here yet.")
            return

        vc1, vc2 = st.columns([1, 3])
        with vc1:
            st.session_state.voice_language = st.selectbox(
                "Language", list(ve.LANG_CODES.keys()),
                index=list(ve.LANG_CODES.keys()).index(lang_label),
                key=f"{page_key}_voice_lang")
        lang_code = ve.LANG_CODES[st.session_state.voice_language]

        # ---- Guided walkthrough ----
        st.markdown("**▶️ Guided Walkthrough**")
        wk_key = f"{page_key}_wk_step"
        st.session_state.setdefault(wk_key, 0)
        step = st.session_state[wk_key]
        if walkthrough_segments:
            step = max(0, min(step, len(walkthrough_segments) - 1))
            seg = walkthrough_segments[step]
            st.info(f"**{seg['title']}** ({step + 1}/{len(walkthrough_segments)})\n\n{seg['text']}")
            wb1, wb2, wb3 = st.columns(3)
            with wb1:
                if st.button("🔊 Sunaiye is step ko", key=f"{page_key}_wk_play"):
                    st.components.v1.html(ve.tts_html(seg["text"], lang_code), height=0)
            with wb2:
                if st.button("⬅️ Pichla", key=f"{page_key}_wk_prev", disabled=step == 0):
                    st.session_state[wk_key] = step - 1
                    st.rerun()
            with wb3:
                if st.button("➡️ Agla", key=f"{page_key}_wk_next", disabled=step >= len(walkthrough_segments) - 1):
                    st.session_state[wk_key] = step + 1
                    st.rerun()
        else:
            st.caption("Is page ke liye abhi walkthrough banane layak kuch nahi hai.")

        st.divider()

        # ---- Push-to-talk Q&A ----
        st.markdown(f"**🎤 {assistant_name} se poochiye**")
        api_key = ac.get_api_key() or st.session_state.ai_groq_key
        if not api_key:
            st.caption("⚠️ Ye 🤖 AI Assistant page jaisi hi API key use karta hai — pehle wahan se ek free "
                       "OpenRouter key set karein (Admin Panel), tab yahan bhi kaam karega.")
            return
        transcript = speech_to_text(language=lang_code, start_prompt=f"🎤 Bolo, {assistant_name}",
                                     stop_prompt="⏹️ Ruko", just_once=True, use_container_width=True,
                                     key=f"{page_key}_stt")
        if transcript:
            st.caption(f"Aapne poocha: *{transcript}*")
            if df is not None:
                with st.spinner(f"{assistant_name} soch rahi hai..."):
                    result = ac.ask(transcript, df, meta or {}, kpis or [], dashboard_charts or [], api_key)
                if result.get("error"):
                    st.error(result["error"])
                elif result.get("answer"):
                    st.success(result["answer"])
                    st.components.v1.html(ve.tts_html(result["answer"], lang_code), height=0)
            else:
                st.warning("Data load nahi hai abhi, is sawaal ka jawab nahi de sakte.")


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
            if lo == hi:
                continue  # a single-value range isn't a useful filter (matches the numeric filter's lo==hi skip above)
            with cols_ui[i % 3]:
                default = filters.get(f"{key_prefix}{col}_daterange", (lo, hi))
                # st.date_input can hand back a 1-item tuple while someone has picked
                # only the START of a range (before picking the end date) - feeding
                # that partial tuple back in as `default` on the next rerun crashes
                # with a StreamlitAPIException (it only accepts None, a single date,
                # or an exact 2-item tuple/list). That stored bad value also
                # persists to disk, so it kept crashing on every load, for every
                # role, until cleared. Guard against it (and any other malformed
                # leftover value, e.g. from a column that no longer has the same
                # date range after a database refresh) by falling back to the full
                # range instead of ever handing st.date_input something invalid.
                if not (isinstance(default, (tuple, list)) and len(default) == 2):
                    default = (lo, hi)
                # Belt-and-braces: the guard above covers the one specific bad shape
                # we could reproduce, but st.date_input can reject a stored value for
                # other reasons too (e.g. a date outside its own internal allowed
                # range). Whatever the reason, ONE broken date filter should never
                # take down cards/charts on the ENTIRE page for every role - that's
                # strictly worse than just not offering a filter for this one column.
                try:
                    rng = st.date_input(col, default, key=f"{key_prefix}filt_date_{col}")
                except Exception:
                    filters.pop(f"{key_prefix}{col}_daterange", None)
                    try:
                        rng = st.date_input(col, (lo, hi), key=f"{key_prefix}filt_date_{col}_safe")
                    except Exception:
                        st.caption(f"⚠️ Couldn't build a date filter for **{col}** (unusual date values) — skipped.")
                        i += 1
                        continue
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
                if lo == hi:
                    continue
                try:
                    rng = st.date_input(field, (lo, hi), key=skey, label_visibility="collapsed")
                except Exception:
                    st.caption(f"⚠️ Couldn't build a date slicer for **{field}** (unusual date values) — skipped.")
                    continue
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


def kpi_cards(kpis, pinnable=False, key_prefix="", df=None, filterable=False, removable=False):
    """Renders KPI cards in a responsive grid. If pinnable, shows a pin checkbox per card.
    If filterable (needs `df`), every card that represents a plain column aggregation
    (has "column"/"agg" set — see de.compute_kpis) gets its OWN "⚙️ Format & Filter" popover,
    completely independent of every other card: its own filter, AND its own number format
    (one card can show "58.18 L", the next "5,818,432.00", another "12.3%" — no single
    "global" setting forces every card to look the same).
    If removable, shows a 🗑️ button that unpins the card right here (e.g. on the Boss
    Dashboard) — same effect as un-ticking its ⭐ back on Raw Analysis, but without
    having to leave the page to do it."""
    n_cols = 4
    filter_store = st.session_state.p1_kpi_filters
    format_store = st.session_state.p1_kpi_format
    custom_store = st.session_state.p1_kpi_custom_code
    format_labels = list(ms.NUMBER_FORMAT_PRESETS.keys())
    for row_start in range(0, len(kpis), n_cols):
        cols = st.columns(n_cols)
        for j, k in enumerate(kpis[row_start:row_start + n_cols]):
            with cols[j]:
                label = k["label"]
                value, sub = k["value"], k.get("sub")
                if filterable and df is not None and k.get("column") and k.get("agg"):
                    card_filters = filter_store.get(label, [])
                    fmt_choice = format_store.get(label, "Auto (Cr / L / K)")
                    custom_code = custom_store.get(label, "#,##0.00")
                    fdf = ms.apply_filters(df, card_filters) if card_filters else df
                    col, agg = k["column"], k["agg"]
                    s = pd.to_numeric(fdf[col], errors="coerce") if agg in ("sum", "mean") else fdf[col]
                    if agg == "sum":
                        raw = s.sum()
                    elif agg == "mean":
                        raw = s.mean()
                    else:  # nunique
                        raw = s.nunique()
                    value = ms.format_value(raw, ms.NUMBER_FORMAT_PRESETS.get(fmt_choice, "auto"), custom_code)
                    if card_filters:
                        sub = f"{sub} · {len(fdf):,} rows after this card's filter"
                    st.metric(label, value, help=sub)
                    with st.popover("⚙️ Format & Filter", use_container_width=True):
                        st.caption("Number format — this card only")
                        new_fmt = st.selectbox("Format", format_labels,
                                                index=format_labels.index(fmt_choice) if fmt_choice in format_labels else 0,
                                                key=f"{key_prefix}kpifmt_{label}", label_visibility="collapsed")
                        format_store[label] = new_fmt
                        if ms.NUMBER_FORMAT_PRESETS.get(new_fmt) == "custom":
                            custom_store[label] = st.text_input(
                                "Excel-style code (e.g. #,##0.00 or 0.0% or ₹#,##0,,\"M\")",
                                value=custom_code, key=f"{key_prefix}kpicustom_{label}")
                        st.divider()
                        st.caption("Filter — this card only")
                        new_filters = be.render_filter_builder(df, card_filters, key_prefix=f"{key_prefix}kpi_{label}_")
                        filter_store[label] = new_filters
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
    _text_html = (
        f"<div style='font-size:{_b['font_size']}px; color:{_b['color']}; "
        f"font-weight:{'700' if _b['bold'] else '400'}; "
        f"font-style:{'italic' if _b['italic'] else 'normal'}; "
        f"font-family:{_b['font_family']}; margin-bottom:0.2rem;'>{_b['text']}</div>"
    )
    _render_glow_target("brand_glow_text", "text", _b, _text_html)
    st.caption(f"Logged in as **{st.session_state.username}** ({st.session_state.role})")
    st.caption(f"💡Tool Guidance - ⚙️Settings > ❓how this tool work")

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

    nav_options = ["📥 Connect Data", "📊 Raw Analysis", "🧩 Custom Builder", "⭐ Boss Dashboard",
                    "📈 Full Analysis", "🗂 Data Table", "🤖 AI Assistant", "⚙️ Settings", "💎 Plans"]
    # "💡 Business Insights" only makes sense for datasets that actually have a
    # Payment Page Title-shaped column (e.g. "Badminton AMD Mondays") - this app
    # is used by many different clients with unrelated datasets, so the tab
    # itself stays hidden unless THIS client's currently loaded data has one.
    if st.session_state.df_raw is not None and ppt.detect_title_column(st.session_state.df_raw, st.session_state.meta):
        nav_options.insert(4, "💡 Business Insights")  # right after Boss Dashboard
    if st.session_state.role == auth.ROLE_REPORT_VIEWER:
        nav_options = ["⭐ Boss Dashboard"]   # nothing else exists for this account, not even Settings
    if st.session_state.role == auth.ROLE_ADMIN:
        nav_options.append("🔐 Admin Panel")
    page = st.radio("Navigate", nav_options, label_visibility="collapsed")
    st.session_state.page = page
    st.divider()
    if st.session_state.plan == "free" and st.session_state.role != auth.ROLE_ADMIN:
        _trial_sb = auth.get_trial_status(st.session_state.username)
        if _trial_sb["days_left"] is not None:
            st.caption(f"🆓 Free trial: **{_trial_sb['days_left']} day(s) left**")
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
        _clear_session_cookie()
        st.rerun()


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

        if st.session_state.data_source_is_db:
            db_col1, db_col2, db_col3 = st.columns([2, 1, 2])
            with db_col1:
                if st.session_state.db_last_refreshed_at:
                    ago = int(time.time() - st.session_state.db_last_refreshed_at)
                    ago_txt = f"{ago}s ago" if ago < 60 else f"{ago // 60}m ago"
                    st.caption(f"🔌 Connected live to **{st.session_state.db_last_load_label}** — last refreshed {ago_txt}.")
                else:
                    st.caption(f"🔌 Connected live to **{st.session_state.db_last_load_label}**.")
            with db_col2:
                if st.button("🔄 Refresh now", key="db_manual_refresh_btn", use_container_width=True):
                    ok, msg = _run_db_refresh()
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.rerun()
            with db_col3:
                sync_choice = st.selectbox(
                    "Auto-sync", ["Off", "Every 10s", "Every 30s", "Every 1 min", "Every 5 min"],
                    index=["Off", "Every 10s", "Every 30s", "Every 1 min", "Every 5 min"].index(
                        {0: "Off", 10: "Every 10s", 30: "Every 30s", 60: "Every 1 min", 300: "Every 5 min"}
                        .get(st.session_state.db_auto_sync_seconds, "Off")
                    ),
                    key="db_auto_sync_choice", label_visibility="collapsed",
                    help="Automatically re-run the same query and pull in new rows, without clicking Refresh.",
                )
                st.session_state.db_auto_sync_seconds = {
                    "Off": 0, "Every 10s": 10, "Every 30s": 30, "Every 1 min": 60, "Every 5 min": 300,
                }[sync_choice]
            if st.session_state.db_auto_sync_seconds and AUTOREFRESH_AVAILABLE:
                st_autorefresh(interval=st.session_state.db_auto_sync_seconds * 1000, key="db_connect_page_auto_sync")
                ok, msg = _run_db_refresh()
                if not ok:
                    st.error(msg)
            elif st.session_state.db_auto_sync_seconds and not AUTOREFRESH_AVAILABLE:
                st.warning("Auto-sync needs the `streamlit-autorefresh` package — add it to requirements.txt to enable this.")

        if can_edit() and st.button("🗑️ Clear loaded data (start over with a new file/database)"):
            st.session_state.df_raw = None
            st.session_state.meta = None
            st.session_state.data_source_name = None
            st.session_state.filters = {}
            st.session_state.dashboard_charts = []
            st.session_state.pinned_kpis = []
            st.session_state.dashboard_slicers = []
            st.session_state._last_upload_sig = None
            st.session_state.data_source_is_db = False
            st.session_state.db_last_load_sql = ""
            st.session_state.db_last_load_label = ""
            st.session_state.db_auto_sync_seconds = 0
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
                sample_choice = st.selectbox("Try with sample data", list(SAMPLE_DATASETS.keys()),
                                              key="sample_dataset_choice", label_visibility="collapsed")
                if st.button("🎯 Load Sample Data", use_container_width=True):
                    load_sample(SAMPLE_DATASETS[sample_choice])
                    st.rerun()
                st.caption("3 industries available — proves this works on any data, not just one kind.")

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
                            st.session_state.data_source_is_db = True
                            st.session_state.db_last_load_sql = custom_sql
                            st.session_state.db_last_load_label = f"{st.session_state.db_conn_type}: {pick_table}"
                            st.session_state.db_last_refreshed_at = time.time()
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
                   "**⚙️ Format & Filter** — pick General/Number/Currency/Percentage/Custom for THAT card "
                   "only, plus an optional filter — independent of the page filter above and of every other card.")
    kpis = de.compute_kpis(df, meta)
    kpi_cards(kpis, pinnable=can_edit(), key_prefix="p1_", df=df, filterable=True)

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

    # Manual sync, on purpose: a client and their linked report-viewer(s) are
    # separate browser sessions sharing this workspace. Whoever clicks this
    # pulls in the OTHER side's latest changes (pinned/swapped charts,
    # slicers, theme...) right away. This replaces an earlier every-1-second
    # auto-refresh, which kept re-syncing from disk in the background even
    # when nothing had changed - a bigger drag on the page than just
    # clicking Refresh when you actually want the latest.
    _title_col, _refresh_col = st.columns([5, 1])
    with _refresh_col:
        if st.button("🔄 Refresh", use_container_width=True,
                      help="Pull in the latest changes made by the client or their linked viewer(s)"):
            sync_workspace_from_disk(force=True)
            st.rerun()

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

    pinned_custom_charts = [c for c in st.session_state.custom_charts if c.get("pinned")]
    _pinned_kpi_lines = [f"{k['label']}: {k.get('value_display', k.get('value'))}" for k in pinned]
    _n_charts = len(st.session_state.dashboard_charts) + len(pinned_custom_charts)
    render_voice_assistant("boss", ve.walkthrough_boss_dashboard(_pinned_kpi_lines, _n_charts),
                            df=df, meta=meta, kpis=all_kpis, dashboard_charts=st.session_state.dashboard_charts)

    st.divider()

    # ---- Selected charts ----
    st.subheader("Selected Charts")
    chart_png_items = []

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
                if table_df is not None:
                    st.dataframe(table_df, use_container_width=True, height=380)
                elif fig is not None:
                    fig = _render_chart_with_zoom(fig, zoom_key=f"custom_{chart['id']}",
                                                   widget_key=f"p2_custom_{chart['id']}",
                                                   editable=can_edit_dashboard())
                st.caption(f"💡 {insight}")
                # NOTE: we deliberately do NOT render this to a PNG here. Turning a chart into an
                # image (for the PDF) needs kaleido, which is slow (roughly half a second to a
                # couple of seconds PER chart) — and Streamlit re-runs this whole page on every
                # single click anywhere on it. Doing that PNG work on every rerun made the page
                # feel laggy in proportion to how many charts were pinned, even when nobody was
                # exporting anything. We only pay that cost once, when "Generate & Download PDF"
                # is actually clicked, below.
                chart_png_items.append({"title": chart.get("title", "Custom Chart"), "insight": insight,
                                         "fig": fig, "type": chart.get("type", "Chart")})

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
                fig = _render_chart_with_zoom(fig, zoom_key=widget_key, widget_key=f"p2_{widget_key}",
                                               editable=can_edit_dashboard())
                st.caption(f"💡 {insight}")

                chart_png_items.append({"title": variant.get("title", fam), "insight": insight,
                                         "fig": fig, "type": fam})

        st.divider()
        st.subheader("📄 Export")
        _pdf_remaining = ul.remaining(st.session_state.workspace_id, st.session_state.plan, "pdf_exports")
        if _pdf_remaining is not None:
            st.caption(f"🆓 Free plan: {_pdf_remaining} PDF export(s) left today. "
                      f"Free-plan PDFs carry a small watermark — upgrade to Standard for clean, "
                      f"client-ready exports.")
        report_title = st.text_input("Report title", st.session_state.dashboard_name or "Sports Performance & Payments Report")
        subtitle = st.text_input("Subtitle", f"Prepared for management review — {pd.Timestamp.today().date()}")
        filters_summary = ", ".join(
            [f"{k.replace('p2_','')}: {v}" for k, v in st.session_state.filters.items()
             if k.startswith("p2_") and v not in (None, [], ())]
        ) or "None"

        if st.button("⬇️ Generate & Download PDF", type="primary"):
            pdf_ok, pdf_limit_msg = ul.check_and_increment(st.session_state.workspace_id, st.session_state.plan, "pdf_exports")
            if not pdf_ok:
                st.error(f"🚫 {pdf_limit_msg}")
                st.stop()
            render_errors = []
            with st.spinner(f"Rendering {len(chart_png_items)} chart(s) to images for the PDF... "
                             f"(this is the only step that needs to be slow)"):
                for item in chart_png_items:
                    fig = item.pop("fig", None)
                    item["png_bytes"] = None
                    if fig is not None:
                        try:
                            item["png_bytes"] = fig.to_image(format="png", width=1400, height=700, scale=2)
                        except Exception as e:
                            render_errors.append(f"{item['title']}: {e}")
            if render_errors:
                with st.expander(f"⚠️ {len(render_errors)} chart(s) could not be rendered as images "
                                  f"and will be missing from the PDF — click to see why"):
                    for msg in render_errors:
                        st.caption(msg)

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
                    watermark=("FREE TRIAL — UPGRADE FOR CLEAN REPORTS"
                              if st.session_state.plan == "free" else None),
                )
            st.download_button("📥 Click to download report.pdf", data=pdf_bytes,
                                file_name="sports_analytics_report.pdf", mime="application/pdf",
                                type="primary")


# ==================================================================================
# PAGE 2.5: INTELLIGENCE REPORT — full auto business-analytics report on ANY dataset
# ==================================================================================
elif page == "💡 Business Insights":
    st.title("💡 Business Insights")
    st.caption("Payment Page Title ko **Sport → Code/Location → Day** hierarchy me todkar dikhata hai — "
               "kaunsa sport/location/day sabse zyada revenue de raha hai, kaunsi payment pages abhi "
               "active hain, aur kahan focus/reduce karna chahiye. Sirf tab dikhta hai jab dataset me "
               "aisa title column ho.")

    if st.session_state.df_raw is None:
        st.info("⬅️ No data loaded yet. Go to **📥 Connect Data** in the sidebar to upload a file or connect a database.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    df = render_filters(df_raw, meta, key_prefix="ppt_")
    df = render_slicers(df, meta, key_prefix="ppt_", editable=can_edit())
    if df.empty:
        st.warning("No rows match the current filters.")
        st.stop()

    title_col = ppt.detect_title_column(df, meta)
    if not title_col:
        # Belt-and-suspenders: the nav item itself is already hidden when this
        # returns None, but filters/slicers above could in principle narrow
        # the dataset down to nothing usable — same fallback either way.
        st.info("Is dataset me koi 'Payment Page Title' jaisa column nahi mila (Sport/Code/Day pattern wala), "
                "isliye ye page abhi kuch dikha nahi sakta.")
        st.stop()

    auto_status_col = ppt.detect_status_column(df, meta)
    auto_roles = ie.detect_roles(df, meta)
    with st.expander("🧭 Column Mapping — auto-detected, confirm ya change karein", expanded=False):
        numeric_opts = list(meta.get("numeric_cols", []) or []) or list(df.columns)
        c1, c2, c3 = st.columns(3)
        with c1:
            amt_default = auto_roles.get("revenue") if auto_roles.get("revenue") in numeric_opts else numeric_opts[0]
            amount_col = st.selectbox("Amount / Revenue column", numeric_opts,
                                       index=numeric_opts.index(amt_default), key="ppt_amount_col")
        with c2:
            status_opts = ["(none)"] + list(df.columns)
            status_default = auto_status_col or "(none)"
            picked = st.selectbox("Status column (Captured / Failed / Refunded)", status_opts,
                                   index=status_opts.index(status_default) if status_default in status_opts else 0,
                                   key="ppt_status_col")
            status_col = None if picked == "(none)" else picked
        with c3:
            date_opts = ["(none)"] + list(meta.get("date_cols", []) or [])
            date_default = auto_roles.get("date") if auto_roles.get("date") in date_opts else "(none)"
            picked_d = st.selectbox("Date column", date_opts,
                                     index=date_opts.index(date_default), key="ppt_date_col")
            date_col = None if picked_d == "(none)" else picked_d
        st.caption(f"Detected Payment Page Title column: **{title_col}**"
                   + (" · no Status column found — every row treated as Captured" if not status_col else ""))

    edf = ppt.enrich(df, title_col, status_col)
    n_unparsed = int((edf["_parse_status"] == "DATA REVIEW REQUIRED").sum())

    sport_t = ppt.sport_table(edf, amount_col, date_col)
    code_t = ppt.code_table(edf, amount_col, date_col)
    day_t = ppt.day_table(edf, amount_col, date_col)
    page_t = ppt.payment_page_table(edf, title_col, amount_col, date_col)
    hs = ppt.health_score(page_t)

    # ---- KPI row ----
    total_rev = float(page_t["Captured Revenue"].sum()) if not page_t.empty else 0.0
    total_txn = int(page_t["Transactions"].sum()) if not page_t.empty else 0
    total_captured = int(page_t["Captured Transactions"].sum()) if not page_t.empty else 0
    capture_rate = round(100 * total_captured / total_txn, 1) if total_txn else 0
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Captured Revenue", f"₹{total_rev:,.0f}")
    k2.metric("Captured Transactions", f"{total_captured:,}")
    k3.metric("Capture Rate", f"{capture_rate}%")
    k4.metric("Unique Payment Pages", f"{page_t['Payment Page Title'].nunique():,}" if not page_t.empty else "0")
    k5.metric("Health Score", f"{hs['score']} · {hs['label']}" if hs["score"] is not None else hs["label"])

    if n_unparsed:
        st.warning(f"⚠️ {n_unparsed:,} row(s) ka title format samajh nahi aaya (Sport/Code/Day pattern match nahi hua) "
                    "— unhe **DATA REVIEW REQUIRED** maana gaya hai, guess nahi kiya gaya.")

    def _top_row(table, name_col):
        if table is None or table.empty:
            return None
        r = table.iloc[0]
        return {"name": r[name_col], "revenue": r.get("Captured Revenue", 0)}

    decisions = ppt.management_decisions(sport_t, code_t, day_t, page_t)
    _wk_bi = ve.walkthrough_business_insights(
        _top_row(sport_t, "Sport"), _top_row(code_t, "Code / Location"), _top_row(day_t, "Day"),
        hs, len(decisions))
    render_voice_assistant("bi", _wk_bi, df=df, meta=meta, kpis=de.compute_kpis(df, meta),
                            dashboard_charts=st.session_state.dashboard_charts)

    st.divider()
    st.subheader("🏆 Sport Analysis")
    st.dataframe(sport_t, use_container_width=True, hide_index=True)

    st.subheader("📍 Code / Location Analysis")
    st.dataframe(code_t, use_container_width=True, hide_index=True)

    st.subheader("📅 Day Analysis")
    st.dataframe(day_t, use_container_width=True, hide_index=True)

    st.subheader("🧾 Payment Page Analysis (detailed)")
    st.dataframe(page_t, use_container_width=True, hide_index=True, height=420)

    st.divider()
    st.subheader("❤️ Health Score")
    hcol1, hcol2 = st.columns([1, 3])
    with hcol1:
        st.metric("Overall", f"{hs['score']}" if hs["score"] is not None else "—", hs["label"])
    with hcol2:
        if hs["components"]:
            st.dataframe(pd.DataFrame(hs["components"]), use_container_width=True, hide_index=True)
        else:
            st.caption("DATA REQUIRED — not enough of this dataset's columns matched to score health.")

    st.divider()
    st.subheader("🧭 Management Decisions")
    st.caption("Evidence-based hi — koi bhi decision revenue ke alawa capacity/cost/profit data maangta hai "
               "to wo explicitly 'DATA REQUIRED' bolega, guess nahi karega.")
    if not decisions:
        st.info("Abhi koi strong 🟢/🟡/🔴 signal nahi mila — data thoda aur accumulate hone dein.")
    else:
        st.dataframe(pd.DataFrame(decisions).head(25), use_container_width=True, hide_index=True)

elif page == "📈 Full Analysis":
    st.title("📈 Full Analysis")
    st.caption("Har number Python khud calculate karta hai (koi invented figure nahi). "
               "**Page 1** neeche poora detailed analysis dikhata hai (data samajhna, saaf karna, "
               "aur calculated columns), **Page 2** usका simple summary — past kya tha, future me kya hoga, "
               "aur kahan focus/invest karna hai.")

    if st.session_state.df_raw is None:
        st.info("⬅️ No data loaded yet. Go to **📥 Connect Data** in the sidebar to upload a file or connect a database.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    df = render_filters(df_raw, meta, key_prefix="intel_")
    df = render_slicers(df, meta, key_prefix="intel_", editable=can_edit())
    if df.empty:
        st.warning("No rows match the current filters.")
        st.stop()

    api_key = ac.get_api_key() or st.session_state.ai_groq_key

    # ------------------------------------------------------------------------
    # COLUMN MAPPING — auto-detected, always user-confirmable/editable
    # ------------------------------------------------------------------------
    auto_roles = ie.detect_roles(df, meta)
    saved_overrides = st.session_state.intel_role_overrides or {}
    role_labels = {
        "revenue": "Revenue / Sales", "cost": "Cost", "profit": "Profit (agar already column hai)",
        "customer": "Customer", "product": "Product / Category", "location": "Location / Region",
        "channel": "Channel", "order_id": "Order / Transaction ID", "date": "Date",
    }
    with st.expander("🧭 Column Mapping — auto-detected, confirm ya change karein", expanded=False):
        cols_options = ["(none)"] + list(df.columns)
        new_roles = {}
        rcols = st.columns(3)
        for i, (key, label) in enumerate(role_labels.items()):
            default = saved_overrides.get(key, auto_roles.get(key)) or "(none)"
            idx = cols_options.index(default) if default in cols_options else 0
            with rcols[i % 3]:
                picked = st.selectbox(label, cols_options, index=idx, key=f"intel_role_{key}")
            new_roles[key] = None if picked == "(none)" else picked
        if st.button("💾 Save mapping & Recalculate", key="intel_save_roles", disabled=not can_edit()):
            st.session_state.intel_role_overrides = new_roles
            st.session_state._intel_cache_key = None
            ws.save_light(st.session_state, st.session_state.workspace_id)
            st.rerun()
    roles = dict(auto_roles)
    roles.update({k: v for k, v in saved_overrides.items()})

    # ------------------------------------------------------------------------
    # LANGUAGE TOGGLE (only affects the optional AI write-up on Page 2)
    # ------------------------------------------------------------------------
    top_l, top_r = st.columns([4, 1])
    with top_r:
        language = st.radio("Language", ["English", "Hindi"], horizontal=True,
                             index=0 if st.session_state.intel_language == "English" else 1,
                             key="intel_lang_radio", label_visibility="collapsed")
    if language != st.session_state.intel_language:
        st.session_state.intel_language = language
        st.session_state._intel_narrative = None  # force re-generation in the new language
        ws.save_light(st.session_state, st.session_state.workspace_id)

    # ------------------------------------------------------------------------
    # COMPUTE FACTS (cached — recomputed only when data/mapping/language changes)
    # ------------------------------------------------------------------------
    cache_key = ie.facts_hash(df, roles, st.session_state.intel_language)
    if st.session_state._intel_cache_key != cache_key or st.session_state._intel_facts is None:
        with st.spinner("Numbers calculate ho rahe hain..."):
            st.session_state._intel_facts = ie.build_facts_bundle(df, meta, roles)
        st.session_state._intel_cache_key = cache_key
        st.session_state._intel_narrative = None  # numbers changed -> old narrative is stale
    facts = st.session_state._intel_facts

    # Deterministic insights/recommendations + derived columns — computed once here,
    # used by BOTH pages below. Never depends on an API key.
    ir = ie.generate_insights_and_recommendations(facts)
    enriched_df, derived_log = ie.derive_analysis_columns(df, roles)
    cleaning_log = ie.data_cleaning_log(df, enriched_df, facts["quality"])

    health = facts["health"]
    badge = {"Healthy": "🟢", "Stable": "🟡", "At Risk": "🟠", "Critical": "🔴"}.get(health["label"], "⚪")

    snapshots = ws.load_intel_snapshots(st.session_state.workspace_id)
    snap_caption = None
    if snapshots:
        last = snapshots[-1]
        cur_rev = facts["financials"].get("total_revenue")
        if cur_rev is not None and last.get("total_revenue"):
            delta = 100 * (cur_rev - last["total_revenue"]) / last["total_revenue"]
            when = datetime.datetime.fromtimestamp(last["ts"]).strftime("%d %b %Y")
            snap_caption = f"📊 vs your last saved report ({when}): revenue **{delta:+.1f}%**"

    st.divider()

    # ------------------------------------------------------------------------
    # PAGE NAVIGATION — 2 pages, as requested
    # ------------------------------------------------------------------------
    part = st.radio("Report section", ["📋 Page 1 — Full Analysis", "📈 Page 2 — Summary & Recommendations"],
                     horizontal=True, index=st.session_state.intel_part - 1, key="intel_part_radio",
                     label_visibility="collapsed")
    st.session_state.intel_part = 1 if part.startswith("📋 Page 1") else 2

    _missing_roles = [role_labels[k] for k in ("revenue", "date", "customer") if not roles.get(k)]
    _f = facts["financials"]
    _kpi_lines = []
    if _f.get("total_revenue") is not None:
        _kpi_lines.append(f"Total revenue {de._fmt_num(_f['total_revenue'])}")
    if _f.get("total_orders"):
        _kpi_lines.append(f"{_f['total_orders']:,} total orders")
    if _f.get("customer_count"):
        _kpi_lines.append(f"{_f['customer_count']:,} customers")
    _wk = ve.walkthrough_full_analysis(_missing_roles, _kpi_lines, st.session_state._intel_narrative)
    render_voice_assistant("intel", _wk, df=df, meta=meta, kpis=de.compute_kpis(df, meta),
                            dashboard_charts=st.session_state.dashboard_charts)
    st.divider()

    # ==========================================================================
    # PAGE 1 — DATA UNDERSTANDING, CLEANING, CALCULATED COLUMNS, FULL ANALYSIS
    # ==========================================================================
    if st.session_state.intel_part == 1:
        f = facts["financials"]
        st.markdown(f"##### {facts['row_count']:,} rows × {facts['col_count']} columns"
                    + (f" · date range detected via **{roles.get('date')}**" if roles.get("date") else ""))
        st.markdown(f"### {badge}  Business Health: **{health['label']}**")
        if health["reasons"]:
            st.caption(" • ".join(health["reasons"]))
        if snap_caption:
            st.caption(snap_caption)

        # ---- KPI colored band (matches the reference report's KPI-band look) ----
        kpi_band = [
            ("Total Revenue", de._fmt_num(f.get("total_revenue")) if f.get("total_revenue") is not None else "—", "#2C6E49"),
            ("Total Orders", f"{f.get('total_orders'):,}" if f.get("total_orders") else "—", "#3969AC"),
            ("Customers", f"{f.get('customer_count'):,}" if f.get("customer_count") else "—", "#7F3C8D"),
            ("Avg Order Value", de._fmt_num(f.get("avg_order_value")) if f.get("avg_order_value") is not None else "—", "#E68310"),
        ]
        band_cols = st.columns(len(kpi_band))
        for col, (label, value, color) in zip(band_cols, kpi_band):
            with col:
                st.markdown(
                    f"<div style='background:{color};color:white;padding:6px 10px;border-radius:6px 6px 0 0;"
                    f"font-size:12px;font-weight:600;text-align:center;'>{label}</div>"
                    f"<div style='background:rgba(127,127,127,0.08);padding:14px 10px;border-radius:0 0 6px 6px;"
                    f"text-align:center;font-size:22px;font-weight:700;'>{value}</div>",
                    unsafe_allow_html=True,
                )
        if f.get("profit_calculable"):
            st.caption(f"Total Profit: **{de._fmt_num(f.get('total_profit'))}**"
                       + (f" · Margin: **{f.get('profit_margin_pct')}%**" if f.get("profit_margin_pct") is not None else "")
                       + (f" · Revenue/Customer: **{de._fmt_num(f.get('revenue_per_customer'))}**" if f.get("revenue_per_customer") is not None else ""))

        st.divider()
        st.subheader("🧹 Data Understanding & Cleaning")
        du1, du2 = st.columns(2)
        with du1:
            st.markdown("**What the data looks like**")
            for line in cleaning_log:
                st.caption(f"• {line}")
            st.markdown(f"**Data Quality Score: {facts['quality']['score']}/100**")
            for issue in facts["quality"]["issues"]:
                st.caption(f"• {issue}")
        with du2:
            st.markdown("**Calculated / added columns for this analysis**")
            for line in derived_log:
                st.caption(f"• {line}")
            detected = ", ".join(f"{k}={v}" for k, v in roles.items() if v) or "—"
            st.caption(f"Detected column roles: {detected}")

        st.divider()
        st.subheader("📈 Revenue Trend & Forecast")
        t, fc = facts["trend"], facts["forecast"]
        if t.get("available"):
            trend_df = pd.DataFrame({"Period": t["periods"], "Revenue (Actual)": t["values"]}).set_index("Period")
            st.line_chart(trend_df)
            tc1, tc2 = st.columns(2)
            tc1.caption(f"📈 Best period: **{t['best_period']}** ({de._fmt_num(t['best_period_value'])})")
            tc2.caption(f"📉 Worst period: **{t['worst_period']}** ({de._fmt_num(t['worst_period_value'])})")
            if t.get("overall_change_pct") is not None:
                st.caption(f"Overall change (first → last period): **{t['overall_change_pct']:+.1f}%**"
                           + (f" · CAGR: **{fc.get('cagr_pct')}%**" if t.get("cagr_pct") is not None else ""))
            monthly_tbl = pd.DataFrame({"Period": t["periods"], "Revenue": t["values"], "MoM Growth %": t["mom_growth_pct"]})
            st.markdown("**Month-by-month table**")
            st.dataframe(monthly_tbl, use_container_width=True, hide_index=True)
        else:
            st.info(t.get("reason", "Trend not available."))

        if fc.get("available"):
            st.caption(f"🔮 Forecast (next {len(fc['forecast_periods'])} months) — method: {fc['method']}, "
                       f"confidence: **{fc['confidence']}** (R²={fc['r2']}), direction: **{fc['direction']}**")
            fc_df = pd.DataFrame({
                "Period": fc["history_periods"] + fc["forecast_periods"],
                "Value": fc["history_values"] + fc["forecast_values"],
                "Type": ["Actual"] * len(fc["history_periods"]) + ["Forecast"] * len(fc["forecast_periods"]),
            })
            st.dataframe(fc_df, use_container_width=True, hide_index=True)
            st.caption("⚠️ Rows marked **Forecast** are estimates, not actual results.")
        else:
            st.info(fc.get("reason", "Forecast not available."))

        if facts["anomalies"]:
            st.divider()
            st.subheader("🚨 Anomalies Detected")
            st.dataframe(pd.DataFrame(facts["anomalies"]), use_container_width=True, hide_index=True)

        if facts["correlations"]:
            st.divider()
            st.subheader("📊 Correlations")
            st.dataframe(pd.DataFrame(facts["correlations"]), use_container_width=True, hide_index=True)
            st.caption("Correlation does not prove causation.")

        breakdown_labels = {"product": "📦 Product", "customer": "👥 Customer", "location": "🌍 Location", "channel": "🔀 Channel"}
        any_breakdown = any(facts["breakdowns"].get(k, {}).get("available") for k in breakdown_labels)
        if any_breakdown:
            st.divider()
            st.subheader("🏆 Top / Bottom Breakdowns")
            btabs = st.tabs([lbl for k, lbl in breakdown_labels.items() if facts["breakdowns"].get(k, {}).get("available")])
            available_keys = [k for k in breakdown_labels if facts["breakdowns"].get(k, {}).get("available")]
            for key, tab in zip(available_keys, btabs):
                b = facts["breakdowns"][key]
                with tab:
                    st.caption(f"By **{b['dimension']}**, measured on **{b['measure']}** · {b['unique_count']} unique values · "
                               f"top-5 share of total: **{b['top5_share_pct']}%**")
                    bc1, bc2 = st.columns(2)
                    with bc1:
                        st.markdown("**Top performers**")
                        st.dataframe(pd.DataFrame(b["top"]), use_container_width=True, hide_index=True)
                    with bc2:
                        st.markdown("**Bottom performers**")
                        st.dataframe(pd.DataFrame(b["bottom"]), use_container_width=True, hide_index=True)

        st.divider()
        if st.button("➡️ Continue to Page 2 — Summary & Recommendations", type="primary", key="intel_go_part2"):
            st.session_state.intel_part = 2
            st.rerun()

    # ==========================================================================
    # PAGE 2 — SIMPLE SUMMARY: PAST %, FUTURE %, KEY INSIGHTS, RECOMMENDED ACTIONS
    #          + optional AI write-up + ACTION TRACKER + FOLLOW-UP Q&A + EXPORT
    # ==========================================================================
    else:
        f = facts["financials"]
        st.markdown(f"### {badge}  Business Health: **{health['label']}**")
        if snap_caption:
            st.caption(snap_caption)

        exec_band = [
            ("Total Revenue", de._fmt_num(f.get("total_revenue")) if f.get("total_revenue") is not None else "—", "#2C6E49"),
            ("Total Orders", f"{f.get('total_orders'):,}" if f.get("total_orders") else "—", "#3969AC"),
            ("Customers", f"{f.get('customer_count'):,}" if f.get("customer_count") else "—", "#7F3C8D"),
            ("Profit Margin", f"{f.get('profit_margin_pct')}%" if f.get("profit_margin_pct") is not None else "N/A", "#E68310"),
        ]
        band_cols = st.columns(len(exec_band))
        for col, (label, value, color) in zip(band_cols, exec_band):
            with col:
                st.markdown(
                    f"<div style='background:{color};color:white;padding:6px 10px;border-radius:6px 6px 0 0;"
                    f"font-size:12px;font-weight:600;text-align:center;'>{label}</div>"
                    f"<div style='background:rgba(127,127,127,0.08);padding:14px 10px;border-radius:0 0 6px 6px;"
                    f"text-align:center;font-size:22px;font-weight:700;'>{value}</div>",
                    unsafe_allow_html=True,
                )

        st.divider()
        sp1, sp2 = st.columns(2)
        with sp1:
            st.subheader("⏮️ Past Performance")
            st.write(ir["past_summary"])
        with sp2:
            st.subheader("⏭️ Future Outlook")
            st.write(ir["future_summary"])

        st.divider()
        ins_col, act_col = st.columns(2)
        with ins_col:
            st.subheader("💡 Key Insights")
            for line in ir["key_insights"]:
                st.markdown(f"- {line}")
        with act_col:
            st.subheader("✅ Recommended Actions")
            for i, line in enumerate(ir["recommended_actions"], 1):
                st.markdown(f"{i}. {line}")

        # ---- Deterministic report text, used for download/email below ----
        det_lines = [
            f"# Full Analysis Summary — {pd.Timestamp.today().date()}",
            f"\nBusiness Health: {health['label']}",
            f"\n## Past Performance\n{ir['past_summary']}",
            f"\n## Future Outlook\n{ir['future_summary']}",
            "\n## Key Insights",
            *[f"- {x}" for x in ir["key_insights"]],
            "\n## Recommended Actions",
            *[f"{i}. {x}" for i, x in enumerate(ir["recommended_actions"], 1)],
        ]
        deterministic_report_md = "\n".join(det_lines)

        st.divider()
        exp_col1, exp_col2, exp_col3 = st.columns(3)
        with exp_col1:
            st.download_button("⬇️ Download summary (.md)", data=deterministic_report_md,
                                file_name="analysis_summary.md", mime="text/markdown",
                                use_container_width=True)
        with exp_col2:
            if st.button("💾 Save snapshot for future comparison", use_container_width=True, disabled=not can_edit()):
                ws.save_intel_snapshot({
                    "ts": time.time(),
                    "total_revenue": facts["financials"].get("total_revenue"),
                    "total_profit": facts["financials"].get("total_profit"),
                    "profit_margin_pct": facts["financials"].get("profit_margin_pct"),
                    "row_count": facts["row_count"],
                }, st.session_state.workspace_id)
                st.success("Snapshot saved.")
        with exp_col3:
            with st.popover("📧 Email this summary", use_container_width=True):
                to_email = st.text_input("Send to", key="intel_email_to")
                if st.button("Send", key="intel_email_send") and to_email.strip():
                    ok, msg = es.send_report_email(
                        to_email.strip(), "Analysis Summary — RA-Intelligence Platform",
                        deterministic_report_md)
                    (st.success if ok else st.error)(msg)

        st.divider()
        st.subheader("✅ Action Tracker")
        st.caption("Top Actions ko yahan add karke tick karte jao — persist rahega.")
        new_action = st.text_input("+ Add an action item", key="intel_new_action")
        if st.button("Add", key="intel_add_action") and new_action.strip() and can_edit():
            st.session_state.intel_action_checks.append({"text": new_action.strip(), "done": False})
            ws.save_light(st.session_state, st.session_state.workspace_id)
            st.rerun()
        for i, item in enumerate(list(st.session_state.intel_action_checks)):
            ac1, ac2 = st.columns([9, 1])
            with ac1:
                checked = st.checkbox(item["text"], value=item["done"], key=f"intel_action_{i}", disabled=not can_edit())
                if checked != item["done"]:
                    st.session_state.intel_action_checks[i]["done"] = checked
                    ws.save_light(st.session_state, st.session_state.workspace_id)
            with ac2:
                if can_edit() and st.button("🗑️", key=f"intel_action_del_{i}"):
                    st.session_state.intel_action_checks.pop(i)
                    ws.save_light(st.session_state, st.session_state.workspace_id)
                    st.rerun()

        # ---- Optional AI write-up — deeper narrative, NOT the primary content anymore ----
        st.divider()
        with st.expander("🤖 Ask AI to elaborate further (optional, needs an API key)", expanded=False):
            st.caption("Deterministic summary upar already complete hai. Ye sirf ek extra, longer AI "
                       "write-up hai agar chahiye — sabhi numbers wahi hain, koi naya number invent nahi hota.")
            if not api_key:
                if st.session_state.role == auth.ROLE_ADMIN:
                    st.warning("No free OpenRouter API key configured yet — add one on the **🤖 AI Assistant** page to enable this.")
                else:
                    st.info("🧠 AI write-up abhi enable nahi hai. Please contact your admin to turn this on.")
            else:
                gen_clicked = st.button("✨ Generate / Refresh AI write-up", type="primary", key="intel_gen_report")
                if gen_clicked or (st.session_state._intel_narrative is None and False):
                    ai_ok, ai_limit_msg = ul.check_and_increment(st.session_state.workspace_id, st.session_state.plan, "ai_calls")
                    if not ai_ok:
                        st.error(f"🚫 {ai_limit_msg}")
                        st.stop()
                    with st.spinner("AI report likh raha hai..."):
                        facts_text = ie.facts_to_prompt_text(facts)
                        result = ac.generate_report_narrative(facts_text, api_key, st.session_state.intel_language)
                    if result["error"]:
                        st.error(result["error"])
                    else:
                        st.session_state._intel_narrative = result["report"]
                _ai_remaining = ul.remaining(st.session_state.workspace_id, st.session_state.plan, "ai_calls")
                if _ai_remaining is not None:
                    st.caption(f"🆓 Free plan: {_ai_remaining} AI request(s) left today (shared across AI Assistant + this write-up).")

                if st.session_state._intel_narrative:
                    st.markdown(st.session_state._intel_narrative)
                    st.download_button("⬇️ Download AI write-up (.md)", data=st.session_state._intel_narrative,
                                        file_name="ai_writeup.md", mime="text/markdown", key="intel_ai_dl")

        if api_key:
            st.divider()
            st.subheader("💬 Follow-up Questions")
            st.caption("Is report ke baare mein kuch aur poochna hai? Answers real SQL se grounded hote hain.")
            for turn in st.session_state.intel_qa_history:
                with st.chat_message(turn["role"]):
                    st.markdown(turn["content"])
            qa_question = st.chat_input("e.g. \"Which month had the highest revenue?\"", key="intel_qa_input")
            if qa_question:
                st.session_state.intel_qa_history.append({"role": "user", "content": qa_question})
                with st.chat_message("user"):
                    st.markdown(qa_question)
                with st.chat_message("assistant"):
                    ai_ok, ai_limit_msg = ul.check_and_increment(st.session_state.workspace_id, st.session_state.plan, "ai_calls")
                    if not ai_ok:
                        st.error(f"🚫 {ai_limit_msg}")
                        st.stop()
                    with st.spinner("Sochte hain..."):
                        kpis_for_qa = de.compute_kpis(df, meta)
                        qa_result = ac.ask(qa_question, df, meta, kpis_for_qa, st.session_state.dashboard_charts,
                                           api_key, history=st.session_state.intel_qa_history[:-1])
                    if qa_result["error"]:
                        st.error(qa_result["error"])
                    else:
                        st.markdown(qa_result["answer"])
                        st.session_state.intel_qa_history.append({"role": "assistant", "content": qa_result["answer"]})

        st.divider()
        if st.button("⬅️ Back to Page 1", key="intel_back_part1"):
            st.session_state.intel_part = 1
            st.rerun()


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
               "grounded in real SQL run against your dataset — not guesses — and shown with proof below each reply. "
               "Chat history is kept for **5 days** and then auto-deleted.")

    if st.session_state.df_raw is None:
        st.info("Load data on the **📥 Connect Data** page first.")
        st.stop()

    df_raw = st.session_state.df_raw
    meta = st.session_state.meta
    api_key = ac.get_api_key() or st.session_state.ai_groq_key

    # Pull this workspace's saved chat history (5-day auto-expiring) exactly once
    # per workspace per session — after that, session_state is the live copy.
    if st.session_state._chat_history_loaded_ws != st.session_state.workspace_id:
        st.session_state.ai_chat_history = ws.load_chat_history(st.session_state.workspace_id)
        st.session_state._chat_history_loaded_ws = st.session_state.workspace_id

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
    _ai_remaining_top = ul.remaining(st.session_state.workspace_id, st.session_state.plan, "ai_calls")
    if _ai_remaining_top is not None:
        st.caption(f"🆓 Free plan: {_ai_remaining_top} AI request(s) left today.")

    render_voice_assistant("ai_page", [], df=df_raw, meta=meta, kpis=kpis,
                           dashboard_charts=st.session_state.dashboard_charts)

    for turn in st.session_state.ai_chat_history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn.get("proof_df") is not None:
                with st.expander("🔍 Proof (SQL + data used)"):
                    st.code(turn["sql_used"], language="sql")
                    st.dataframe(turn["proof_df"], use_container_width=True)

    question = st.chat_input("e.g. \"Which record is number 5?\" or \"What's the trend over the last 3 months?\"")
    if question:
        st.session_state.ai_chat_history.append({"role": "user", "content": question, "ts": time.time()})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            ai_ok, ai_limit_msg = ul.check_and_increment(st.session_state.workspace_id, st.session_state.plan, "ai_calls")
            if not ai_ok:
                st.error(f"🚫 {ai_limit_msg}")
                st.stop()
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
                    "sql_used": result["sql_used"], "proof_df": result["proof_df"], "ts": time.time(),
                })
        ws.save_chat_history(st.session_state.ai_chat_history, st.session_state.workspace_id)

    if st.session_state.ai_chat_history and st.button("🗑️ Clear chat"):
        st.session_state.ai_chat_history = []
        ws.save_chat_history([], st.session_state.workspace_id)
        st.rerun()

    # ------------------------------------------------------------------------
    # AUTO-BUILD A KPI CARD OR CHART FROM A PLAIN-LANGUAGE REQUIREMENT
    # (view-only accounts can chat above, but can't add/pin dashboard content)
    # ------------------------------------------------------------------------
    if not can_edit():
        st.stop()

    st.divider()
    st.subheader("🪄 Ask AI to build a card or chart")
    st.caption("Describe what you need — AI designs a matching KPI card or chart from your real "
               "columns. Add it to **🧩 Custom Builder** with one click, or pin it straight to the "
               "**⭐ Boss Dashboard**.")
    ai_req = st.text_input("e.g. \"monthly revenue trend\" or \"top clients by total paid\"", key="ai_card_req")
    if st.button("✨ Generate", key="ai_card_gen") and ai_req.strip():
        with st.spinner("Designing a card/chart from your data..."):
            gen_result = ac.suggest_card_or_chart(ai_req.strip(), df_raw, api_key)
        if gen_result.get("error"):
            st.error(gen_result["error"])
            st.session_state["_ai_card_spec"] = None
        else:
            st.session_state["_ai_card_spec"] = gen_result["spec"]

    ai_spec = st.session_state.get("_ai_card_spec")
    if ai_spec:
        col_names = {c["name"] for c in ms.list_all_columns(df_raw)}
        preview_box = st.container(border=True)
        with preview_box:
            if ai_spec.get("kind") == "kpi" and ai_spec.get("column") in col_names:
                preview_card = {
                    "title": ai_spec.get("title") or "AI Card",
                    "column": ai_spec["column"],
                    "measure": ac.normalize_measure(ai_spec.get("measure")) or "Sum",
                    "filters": [], "number_format": "Auto (Cr / L / K)", "custom_format_code": "#,##0.00",
                }
                be.render_kpi_card_value(df_raw, preview_card)
                pc1, pc2 = st.columns(2)
                with pc1:
                    if st.button("➕ Add to Custom Builder", key="ai_card_add", use_container_width=True):
                        new_card = be.new_kpi_card(df_raw)
                        new_card.update({k: v for k, v in preview_card.items() if k != "filters"})
                        st.session_state.custom_kpis.append(new_card)
                        ws.save_light(st.session_state, st.session_state.workspace_id)
                        st.session_state["_ai_card_spec"] = None
                        st.success("Added! See it on 🧩 Custom Builder → Custom KPI Cards.")
                        st.rerun()
                with pc2:
                    if st.button("📌 Add & Pin to Boss Dashboard", key="ai_card_pin", use_container_width=True):
                        new_card = be.new_kpi_card(df_raw)
                        new_card.update({k: v for k, v in preview_card.items() if k != "filters"})
                        new_card["pinned"] = True
                        st.session_state.custom_kpis.append(new_card)
                        ws.save_light(st.session_state, st.session_state.workspace_id)
                        st.session_state["_ai_card_spec"] = None
                        st.success("Added and pinned to ⭐ Boss Dashboard!")
                        st.rerun()
            elif ai_spec.get("kind") == "chart" and ai_spec.get("x_col") in col_names and ai_spec.get("y_col") in col_names:
                _grain = ac.normalize_grain(ai_spec.get("x_grain"))
                if ai_spec["x_col"] not in (meta.get("date_cols") or []):
                    _grain = None  # a grain only makes sense on an actual date column - never trust the AI blindly here
                preview_chart = {
                    "id": "ai_preview",
                    "title": ai_spec.get("title") or "AI Chart",
                    "type": ac.normalize_chart_type(ai_spec.get("chart_type")) or "Bar",
                    "x_col": ai_spec["x_col"],
                    "x_grain": _grain,
                    "y_col": ai_spec["y_col"],
                    "y_measure": ac.normalize_measure(ai_spec.get("y_measure")) or "Sum",
                    "color_col": ai_spec.get("color_col") if ai_spec.get("color_col") in col_names else None,
                    "filters": [],
                }
                fig, insight, table_df = be.build_custom_figure(df_raw, preview_chart, get_style_dict())
                if table_df is not None:
                    st.dataframe(table_df, use_container_width=True, height=380)
                elif fig is not None:
                    st.plotly_chart(fig, use_container_width=True, key="ai_chart_preview", config=ce.PLOTLY_CONFIG)
                st.caption(f"💡 {insight}")
                pc1, pc2 = st.columns(2)
                with pc1:
                    if st.button("➕ Add to Custom Builder", key="ai_chart_add", use_container_width=True):
                        new_chart = be.new_chart(df_raw)
                        new_chart.update({k: v for k, v in preview_chart.items() if k not in ("id", "filters")})
                        st.session_state.custom_charts.append(new_chart)
                        ws.save_light(st.session_state, st.session_state.workspace_id)
                        st.session_state["_ai_card_spec"] = None
                        st.success("Added! See it on 🧩 Custom Builder → Custom Charts.")
                        st.rerun()
                with pc2:
                    if st.button("📌 Add & Pin to Boss Dashboard", key="ai_chart_pin", use_container_width=True):
                        new_chart = be.new_chart(df_raw)
                        new_chart.update({k: v for k, v in preview_chart.items() if k not in ("id", "filters")})
                        new_chart["pinned"] = True
                        st.session_state.custom_charts.append(new_chart)
                        ws.save_light(st.session_state, st.session_state.workspace_id)
                        st.session_state["_ai_card_spec"] = None
                        st.success("Added and pinned to ⭐ Boss Dashboard!")
                        st.rerun()
            else:
                st.warning("AI suggested a column that doesn't match your dataset — try rephrasing your requirement.")
                st.json(ai_spec)


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
        if st.session_state.role in (auth.ROLE_CLIENT, auth.ROLE_ADMIN):
            st.subheader("🎤 Voice Assistant Name")
            if st.session_state.plan == "standard" or st.session_state.role == auth.ROLE_ADMIN:
                st.caption("This is the name shown/spoken on the 🎤 voice assistant "
                           "(Full Analysis, Business Insights, Boss Dashboard). Leave blank for the default, 'री'.")
                _new_assistant_name = st.text_input("Assistant name", value=st.session_state.assistant_name or "",
                                                     placeholder="री", key="assistant_name_input")
                if st.button("💾 Save assistant name", key="save_assistant_name"):
                    st.session_state.assistant_name = _new_assistant_name.strip() or None
                    ws.save_light(st.session_state, st.session_state.workspace_id)
                    st.success(f"Saved — assistant will now be called "
                               f"'{ve.get_assistant_name('standard', st.session_state.assistant_name)}'.")
            else:
                st.caption(f"On the Free plan, the voice assistant is always called "
                           f"**'{ve.DEFAULT_ASSISTANT_NAME}'**. Custom naming is a 💎 Standard-plan perk.")
            st.divider()

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

            st.markdown(
                f"<div style='font-size:{b['font_size']}px; color:{b['color']}; "
                f"font-weight:{'700' if b['bold'] else '400'}; "
                f"font-style:{'italic' if b['italic'] else 'normal'}; "
                f"font-family:{b['font_family']};'>Preview: {b['text']}</div>",
                unsafe_allow_html=True,
            )
            st.divider()
            st.markdown("**🖼️ Login page logo**")
            st.caption("Shown above the sign-in form. Set a different logo for dark-theme and "
                       "light-theme visitors — upload a PNG / JPG / JPEG / PDF, or paste a direct "
                       "image link (right-click an image anywhere, incl. Google Images results, and "
                       "choose 'Copy image address').")

            def _logo_editor(col, mode, label):
                with col:
                    st.markdown(label)
                    if b.get(f"logo_{mode}"):
                        st.image(b[f"logo_{mode}"], width=160)
                        if st.button("🗑️ Remove logo", key=f"brand_logo_rm_{mode}"):
                            b[f"logo_{mode}"] = None
                            b[f"logo_{mode}_mime"] = None
                            st.rerun()
                    up = st.file_uploader("Upload PNG / JPG / JPEG / PDF", type=["png", "jpg", "jpeg", "pdf"],
                                           key=f"brand_logo_up_{mode}")
                    if up is not None:
                        data, mime = _process_logo_file(up)
                        if data:
                            b[f"logo_{mode}"] = data
                            b[f"logo_{mode}_mime"] = mime
                            st.success("Logo ready below — click 'Save branding for everyone' to publish it.")
                    url = st.text_input("...or paste a direct image link", key=f"brand_logo_url_{mode}",
                                         placeholder="https://...")
                    if st.button("Use this link", key=f"brand_logo_url_btn_{mode}") and url.strip():
                        data, mime = _fetch_logo_from_url(url.strip())
                        if data:
                            b[f"logo_{mode}"] = data
                            b[f"logo_{mode}_mime"] = mime
                            st.success("Logo ready below — click 'Save branding for everyone' to publish it.")

            lg1, lg2 = st.columns(2)
            _logo_editor(lg1, "dark", "🌙 Dark theme logo")
            _logo_editor(lg2, "light", "☀️ Light theme logo")

            b["logo_width"] = st.slider("Logo size on the login page (px wide)", 80, 500,
                                         b.get("logo_width", 220), key="brand_logo_width")

            st.divider()
            st.markdown("**✨ Neon / Glow Lighting**")
            st.caption("An animated glow around the brand text and/or logo — classic neon-sign look, "
                       "fully customizable (not locked to aqua, though that's the default).")
            b["glow_enabled"] = st.toggle("Enable glow lighting", b.get("glow_enabled", False), key="brand_glow_enabled")
            if b["glow_enabled"]:
                gc1, gc2, gc3 = st.columns(3)
                with gc1:
                    b["glow_targets"] = st.multiselect(
                        "Apply glow to", ["text", "logo"], default=b.get("glow_targets", ["text", "logo"]),
                        format_func=lambda x: {"text": "Sidebar brand text", "logo": "Login logo"}[x],
                        key="brand_glow_targets")
                with gc2:
                    b["glow_color"] = st.color_picker("Glow color", b.get("glow_color", "#00E5FF"), key="brand_glow_color")
                with gc3:
                    style_opts = {"steady": "Steady (always-on)", "pulse": "Pulse (breathing)",
                                   "flicker": "Flicker (neon-sign)", "rainbow": "Rainbow cycle"}
                    b["glow_style"] = st.selectbox(
                        "Animation style", list(style_opts.keys()), format_func=lambda k: style_opts[k],
                        index=list(style_opts.keys()).index(b.get("glow_style", "pulse")), key="brand_glow_style")
                gc4, gc5 = st.columns(2)
                with gc4:
                    b["glow_intensity"] = st.slider("Glow intensity (px)", 4, 40, b.get("glow_intensity", 16), key="brand_glow_intensity")
                with gc5:
                    if b["glow_style"] != "steady":
                        b["glow_speed"] = st.slider("Animation speed (seconds per cycle — lower = faster)",
                                                     0.5, 6.0, float(b.get("glow_speed", 2.2)), 0.1, key="brand_glow_speed")

                st.caption("Live preview:")
                _prev_text_html = (
                    f"<div style='font-size:{b['font_size']}px; color:{b['color']}; "
                    f"font-weight:{'700' if b['bold'] else '400'}; "
                    f"font-style:{'italic' if b['italic'] else 'normal'}; "
                    f"font-family:{b['font_family']};'>{b['text']}</div>"
                )
                _render_glow_target("brand_glow_preview_text", "text", b, _prev_text_html)
                _preview_logo = b.get("logo_dark") or b.get("logo_light")
                if _preview_logo and "logo" in b["glow_targets"]:
                    _preview_mime = b.get("logo_dark_mime") or b.get("logo_light_mime") or "image/png"
                    _pc1, _pc2, _pc3 = st.columns([1, 1, 1])
                    with _pc2:
                        _render_glow_target("brand_glow_preview_logo", "logo", b,
                                             _logo_img_html(_preview_logo, _preview_mime, min(b["logo_width"], 220)))

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
- **🔄 Refresh** button: if a linked Report Viewer (or you, in another browser)
  changed something on this same dashboard, click Refresh to pull their latest
  changes in — this only syncs on demand, not automatically in the background.

**💡 Business Insights (new)**
- Only appears in the sidebar when your loaded dataset actually has a
  **Payment Page Title**-shaped column (e.g. `"Badminton AMD Mondays"`) — hidden
  entirely for any dataset that doesn't, so it never gets in the way otherwise.
- Automatically splits that title into **Sport → Code/Location → Day**. Anything
  that can't be reliably split is labelled **"DATA REVIEW REQUIRED"** rather
  than guessed at.
- Gives you Sport, Code/Location, Day, and detailed Payment Page tables — each
  with revenue, transactions, capture/failure/refund rate, average transaction
  value, revenue share, and rank.
- **Health Score**: a weighted score (payment success, failure rate, refund
  rate, current activity) with a full breakdown of how it was calculated.
- **Management Decisions**: a 🟢 Scale / 🟡 Optimize / 🔵 Maintain / 🔴 Reduce
  list, each with the actual evidence behind it. Anything that would need data
  this tool doesn't have (capacity, cost, profit) is labelled **"DATA
  REQUIRED"** instead of guessed.

**📈 Full Analysis**
- A deeper, structured look at the *whole* dataset (not just what's pinned to a
  dashboard): data understanding, cleaning notes, KPI analysis, and a plain-language
  past → present → future summary — in **English or Hindi**.
- Auto-detects which columns are Revenue, Cost, Customer, Product, Date, etc. —
  confirmable/editable in the Column Mapping panel if the auto-detection guesses
  wrong for your data.
- Every number here comes straight from your data — nothing is invented, and
  anything the tool can't determine from what's loaded is labelled **"DATA
  REQUIRED"** rather than assumed.

**🗂 Data Table (Page 4)**
- SQL-style access to the raw data: pick which columns to `SELECT`, add a filter
  in pandas/SQL-like syntax (e.g. `Amount > 100000 and Status == 'Paid'`), sort,
  and export the exact slice you need as CSV.

**🤖 AI Assistant**
- Ask questions about your data in plain language — answers are grounded in
  real SQL run against your actual dataset, not guesses, with the underlying
  query shown as proof under each reply.
- Chat history is saved per workspace and auto-deleted after 5 days.
- Free-plan accounts get a limited number of AI requests per day (see 💎 Plans);
  Standard is unlimited.

**🎤 Voice Assistant (new)**
- Appears as a "🎤 <name>" section on Full Analysis, Business Insights, and
  Boss Dashboard. Default name is **"री"** — Standard-plan clients can rename
  it in Settings.
- **🎤 Push-to-talk Q&A**: tap the mic, ask a question out loud, the answer
  comes back both written and spoken — same SQL-grounded engine as 🤖 AI
  Assistant, just by voice.
- **▶️ Guided Walkthrough**: steps through that page's key numbers out loud,
  one point at a time (Play / Next / Previous) — built entirely from data
  already on the page, nothing invented.
- Supports **English, Hindi, and Gujarati**. English and Hindi recognize/speak
  reliably in most browsers; Gujarati support depends on the browser/device
  and may be less consistent — that's a browser/OS limitation, not something
  this app controls.

**⚙️ Settings**
- Reset the default look of the Boss Dashboard, change your own password, and
  (for client accounts) create Report Viewer logins for your own team.

**💎 Plans**
- Compare Free vs Standard, and see your own current plan status (trial days
  left, or subscription renewal date).
- Upgrade/renew via UPI: pick Monthly or Yearly, pay via any UPI app, then
  submit your transaction reference (UTR) for an admin to verify and approve —
  this is a manual, human-checked step on purpose (see the Admin Panel for why),
  so it can take a little while, not instantly.
- Your plan status refreshes automatically — no need to log out and back in
  after an admin approves your upgrade.

**Performance note:** column detection, KPI math and chart aggregation are all
done with vectorized pandas/NumPy operations, so the same tool comfortably
handles datasets from a few hundred rows up to several million rows. For very
large files, use the row-limit slider on the Data Table page and the filters on
every page to narrow down what's rendered on screen.

**Security note:** your login credentials are never stored in plain text.
        """)


elif page == "💎 Plans":
    st.title("💎 Plans")
    if st.session_state.role != auth.ROLE_ADMIN:
        # Same reasoning as the admin's own 15s "Live" pending-requests
        # refresh: someone sitting on THIS page waiting for their upgrade
        # to be approved shouldn't have to click anything (or log back in)
        # to see it land - just re-check every 20s while they're here.
        if AUTOREFRESH_AVAILABLE:
            st_autorefresh(interval=20 * 1000, key="plans_page_autorefresh")
    if st.session_state.plan == "free" and st.session_state.role != auth.ROLE_ADMIN:
        _trial_pp = auth.get_trial_status(st.session_state.username)
        if _trial_pp["days_left"] is not None:
            st.info(f"🆓 You're on the **Free** plan — **{_trial_pp['days_left']} day(s) left** "
                   f"in your {auth.TRIAL_DAYS}-day trial.")
    elif st.session_state.role != auth.ROLE_ADMIN:
        _sub_pp = auth.get_subscription_status(st.session_state.username)
        if _sub_pp["expires_at"]:
            st.success(f"💎 You're on the **Standard** plan ({(_sub_pp['billing_cycle'] or 'monthly').title()}) "
                      f"— renews/expires in **{_sub_pp['days_left']} day(s)**.")
        else:
            st.success("💎 You're on the **Standard** plan — unlimited, no expiry.")
    st.caption("")
    render_plan_comparison()
    st.divider()

    if st.session_state.role != auth.ROLE_ADMIN:
        if st.session_state.plan == "free":
            if st.button("🚀 Upgrade to Standard", type="primary"):
                upgrade_dialog()
        else:
            if st.button("🔁 Renew / change plan"):
                upgrade_dialog()
            st.caption("You're already on Standard — this is only needed if you want to renew early or switch between Monthly/Yearly.")


elif page == "🔐 Admin Panel":
    if st.session_state.role != auth.ROLE_ADMIN:
        st.error("You don't have access to this page.")
        st.stop()

    st.title("🔐 Admin Panel")
    st.caption("Visible to admin accounts only — report-users never see this page.")

    tab_users, tab_mypw, tab_reset, tab_payments, tab_admin_about = st.tabs(
        ["👥 Manage Accounts", "🔑 Change My Own Password", "🗑️ Reset Workspace Data",
         "💳 Payment Requests", "ℹ️ About This Panel"]
    )

    with tab_users:
        st.subheader("Existing accounts")
        users = auth.list_users()
        st.table([{"Username": u["username"], "Role": u["role"], "Plan": u.get("plan", "standard"),
                   "Data workspace": u["workspace_id"], "Email": u["email"] or "—"} for u in users])

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
            plan_choice = st.radio(
                "Plan", ["Standard (unlimited)", "Free (capped — for trials/demos)"], horizontal=True,
                help=f"Free plan caps: {ul.FREE_PLAN_LIMITS['ai_calls']} AI requests/day, "
                     f"{ul.FREE_PLAN_LIMITS['pdf_exports']} PDF exports/day, "
                     f"{ul.FREE_PLAN_LIMITS['max_rows']:,} row max on loaded data (resets daily at UTC "
                     f"midnight) — AND the whole account stops working after {auth.TRIAL_DAYS} days total, "
                     f"regardless of daily usage, until upgraded to Standard.",
            )

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
                    plan = "free" if plan_choice.startswith("Free") else "standard"
                    workspace_id = None  # defaults to the account's own username
                    if role in (auth.ROLE_VIEWER, auth.ROLE_REPORT_VIEWER) and link_choice and link_choice.startswith("Share data with client: "):
                        workspace_id = link_choice.replace("Share data with client: ", "")
                    auth.create_or_update_user(nu.strip(), np1, role, workspace_id=workspace_id,
                                               email=nu_email.strip(), plan=plan)
                    st.success(f"Account '{nu.strip()}' saved as {role_choice} ({plan_choice})"
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
                _sel_plan = auth.get_plan(sel_user)
                if _sel_plan == "free":
                    _sel_trial = auth.get_trial_status(sel_user)
                    st.caption(f"🆓 Free plan — {_sel_trial['days_left']} day(s) left"
                              + (" (expired)" if _sel_trial["expired"] else ""))
                    if st.button(f"🔄 Reset trial ({auth.TRIAL_DAYS} fresh days)", use_container_width=True):
                        auth.reset_trial(sel_user)
                        st.success(f"Trial reset for '{sel_user}' — {auth.TRIAL_DAYS} fresh days.")
                        st.rerun()
                    if st.button("⬆️ Upgrade to Standard (unlimited)", use_container_width=True):
                        auth.set_plan(sel_user, "standard")
                        st.success(f"'{sel_user}' upgraded to Standard (unlimited) — password unchanged.")
                        st.rerun()
                else:
                    _sel_sub = auth.get_subscription_status(sel_user)
                    if _sel_sub["expires_at"]:
                        st.caption(f"💎 Standard plan ({(_sel_sub['billing_cycle'] or 'monthly').title()}) — "
                                  f"{_sel_sub['days_left']} day(s) left" + (" (expired)" if _sel_sub["expired"] else ""))
                    else:
                        st.caption("💎 Standard plan (permanent — no expiry)")
                    if st.button("⬇️ Move to Free plan", use_container_width=True):
                        auth.set_plan(sel_user, "free")
                        st.success(f"'{sel_user}' moved to Free plan — {auth.TRIAL_DAYS}-day trial starts now.")
                        st.rerun()

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

    with tab_payments:
        st.subheader("UPI payment configuration")
        st.caption("This is the UPI ID + Monthly/Yearly price shown to Free-plan users on the 💎 Plans page "
                  "and on the trial/subscription-expired screens. Leave UPI ID blank to hide the whole "
                  "'Pay via UPI' section (they'll just see 'contact your admin' instead).")
        _pay_cfg = pay.get_config()
        with st.form("payment_config_form"):
            cfg_upi_id = st.text_input("Your UPI ID", value=_pay_cfg["upi_id"],
                                       placeholder="yourname@okhdfcbank / yourname@okicici / etc.")
            cfg_payee_name = st.text_input("Payee name shown to users (optional)", value=_pay_cfg["payee_name"])
            cfg_price_col1, cfg_price_col2 = st.columns(2)
            with cfg_price_col1:
                cfg_price = st.number_input("Monthly price (₹)", min_value=0.0, value=float(_pay_cfg["monthly_price"]), step=50.0)
            with cfg_price_col2:
                cfg_yearly_price = st.number_input("Yearly price (₹)", min_value=0.0, value=float(_pay_cfg["yearly_price"]), step=100.0,
                                                   help="Should normally be less than 12x the monthly price, so 'Save X%' has something to show.")
            if st.form_submit_button("💾 Save payment config", type="primary"):
                pay.set_config(cfg_upi_id, cfg_payee_name, cfg_price, cfg_yearly_price)
                st.success("Saved.")
                st.rerun()

        st.divider()
        st.subheader("⏳ Pending requests")
        auto_col1, auto_col2 = st.columns([3, 1])
        with auto_col2:
            pay_auto_refresh = st.checkbox("🔄 Live (auto-refresh)", value=True, key="pay_auto_refresh",
                                           help="Automatically checks for newly submitted requests every 15 "
                                                "seconds, so you don't have to keep refreshing the page or "
                                                "logging back in to see one show up.")
        if pay_auto_refresh:
            if AUTOREFRESH_AVAILABLE:
                st_autorefresh(interval=15 * 1000, key="pay_requests_autorefresh")
            else:
                st.caption("⚠️ Auto-refresh needs the `streamlit-autorefresh` package — not installed here, "
                          "so this tab still needs a manual refresh for now.")
        with auto_col1:
            st.caption("Someone paid via UPI and submitted their transaction reference. **Verify the UTR "
                  "actually shows up in your own GPay/bank statement before approving** — this is the "
                  "one manual step that keeps this whole flow honest without a payment gateway.")
        pending_reqs = pay.list_requests(pay.STATUS_PENDING)
        if not pending_reqs:
            st.info("No pending requests right now.")
        else:
            for r in pending_reqs:
                with st.container(border=True):
                    rc1, rc2, rc3 = st.columns([2, 2, 1])
                    with rc1:
                        st.markdown(f"**{r['username']}** — ₹{r['amount']:.0f} ({r.get('plan_type', 'monthly').title()})")
                        st.caption(f"Submitted: {datetime.datetime.fromtimestamp(r['submitted_at']).strftime('%d %b %Y %H:%M')}")
                    with rc2:
                        st.code(r["utr"], language=None)
                        if r.get("format_flag"):
                            st.caption(f"⚠️ {r['format_flag']}")
                    with rc3:
                        if st.button("✅ Approve", key=f"pay_approve_{r['id']}", use_container_width=True):
                            ok_dec, msg_dec, uname_dec, plan_type_dec = pay.decide_request(r["id"], True)
                            if ok_dec and uname_dec:
                                duration = pay.PLAN_DURATION_DAYS.get(plan_type_dec, 30)
                                auth.set_plan(uname_dec, "standard", duration_days=duration, billing_cycle=plan_type_dec)
                                st.success(f"'{uname_dec}' upgraded to Standard ({plan_type_dec}, {duration} days).")
                            else:
                                st.error(msg_dec)
                            st.rerun()
                        if st.button("❌ Reject", key=f"pay_reject_{r['id']}", use_container_width=True):
                            pay.decide_request(r["id"], False)
                            st.warning("Rejected.")
                            st.rerun()

        past_reqs = pay.list_requests(pay.STATUS_APPROVED) + pay.list_requests(pay.STATUS_REJECTED) + pay.list_requests(pay.STATUS_REVERSED)
        if past_reqs:
            with st.expander(f"📜 Past decisions ({len(past_reqs)})", expanded=False):
                st.caption("Approved a request by mistake (e.g. only a partial amount actually came through)? "
                          "**Disable** immediately undoes it — the account is moved straight back off Standard "
                          "(back to Free with a fresh trial), and the row below is marked Reversed so you keep "
                          "a record of what happened. This does NOT un-submit the request or refund anything — "
                          "it only fixes the account's access.")
                for r in sorted(past_reqs, key=lambda x: x.get("decided_at") or 0, reverse=True):
                    with st.container(border=True):
                        pc1, pc2, pc3, pc4, pc5 = st.columns([2, 2, 1.4, 1.6, 1.4])
                        pc1.markdown(f"**{r['username']}**")
                        pc1.caption(r["utr"])
                        pc2.markdown(f"₹{r['amount']:.0f} ({r.get('plan_type', 'monthly').title()})")
                        _status_badge = {"approved": "✅ Approved", "rejected": "❌ Rejected",
                                         "reversed": "↩️ Reversed"}.get(r["status"], r["status"].title())
                        pc3.markdown(_status_badge)
                        _decided = datetime.datetime.fromtimestamp(r["decided_at"]).strftime("%d %b %Y %H:%M") if r["decided_at"] else "—"
                        pc4.caption(f"Decided: {_decided}")
                        with pc5:
                            if r["status"] == pay.STATUS_APPROVED:
                                if st.button("🚫 Disable", key=f"pay_reverse_{r['id']}", use_container_width=True):
                                    ok_rev, msg_rev, uname_rev = pay.reverse_decision(r["id"])
                                    if ok_rev and uname_rev:
                                        auth.set_plan(uname_rev, "free")
                                        st.success(f"'{uname_rev}' moved back to Free. {msg_rev}")
                                    else:
                                        st.error(msg_rev)
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
#
# PERF: df_raw only ever changes when THIS session loads a brand-new dataset
# (see _apply_loaded_df/load_sample - both REASSIGN df_raw to a fresh object,
# never mutate it in place), so its Python object identity (id()) reliably
# tells us whether the dataset changed since the last time we wrote it to
# disk. Before this check, EVERY interaction anywhere in the app (pinning a
# KPI, adding a chart, moving a slicer...) re-pickled the whole dataset to
# disk, which is what was causing the reported lag. Now the full save()
# (dataset + config) only runs when the dataset actually changed; every
# other interaction uses save_light(), which is cheap because it never
# touches the dataset.
if st.session_state.authenticated and st.session_state.df_raw is not None:
    _wsid_now = effective_workspace_id()
    if st.session_state.get("_last_saved_df_id") != id(st.session_state.df_raw):
        ws.save(st.session_state, _wsid_now)
        st.session_state._last_saved_df_id = id(st.session_state.df_raw)
    else:
        ws.save_light(st.session_state, _wsid_now)


# footer
st.markdown(
    """
    <style>
    footer {visibility: hidden;}
    </style>
    """,
    unsafe_allow_html=True,
)
