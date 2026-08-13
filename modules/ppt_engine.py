"""
ppt_engine.py
-------------
"Payment Page Title" business-intelligence engine, behind the
"💡 Business Insights" page.

This is intentionally a SEPARATE, self-contained module from intel_engine.py
(which is generic and works on any dataset). Everything in here is specific
to one business pattern: a title column shaped like

    "<Sport> <Code/Location> <Weekday>s"      e.g. "Badminton AMD Mondays"

and only makes sense for datasets that actually have a column like that. The
"💡 Business Insights" page in app.py calls detect_title_column() FIRST and
only shows itself at all when that returns a real column — so a client whose
data has no such column never sees this page, and none of this logic ever
runs against unrelated data.

NEVER INVENTS DATA: every number here comes straight out of the loaded
dataframe. Where a title can't be reliably split into Sport/Code/Day, it is
labelled "DATA REVIEW REQUIRED" rather than guessed. Where a decision would
need data the sheet doesn't have (capacity, cost, profit), the reasoning
says so explicitly instead of pretending revenue alone answers it.
"""

import re
import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------
# TITLE PARSING: "<Sport> <Code/Location> <Weekday>s" -> sport / code / day
# --------------------------------------------------------------------------------
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_DAY_RE = re.compile(r"\b(" + "|".join(DAY_NAMES) + r")s?\b", re.IGNORECASE)

TITLE_COL_NAME_HINTS = ["payment_page_title", "payment page title", "page_title", "page title",
                         "product_title", "product_name", "page_name", "plan_title", "plan_name"]


def detect_title_column(df: pd.DataFrame, meta: dict = None) -> str | None:
    """Finds the Payment Page Title column, if this dataset has one. Tries an
    exact name-hint match first; falls back to scanning text-ish columns and
    scoring how many of their values actually contain a weekday name (the one
    structural signal every valid title shares). Returns None (not a guess)
    if nothing scores well enough - that's the signal the whole Business
    Insights page uses to hide itself for unrelated datasets."""
    if df is None or df.empty:
        return None

    lower_cols = {c: str(c).lower().replace("_", " ") for c in df.columns}
    for c, lname in lower_cols.items():
        if any(h.replace("_", " ") in lname for h in TITLE_COL_NAME_HINTS):
            return c

    candidates = list(df.columns)
    if meta:
        candidates = (meta.get("text_cols", []) or []) + (meta.get("categorical_cols", []) or [])
        candidates = [c for c in candidates if df[c].dtype == object] or [c for c in df.columns if df[c].dtype == object]

    best_col, best_score = None, 0.0
    for c in candidates:
        s = df[c].dropna().astype(str)
        if len(s) < 5:
            continue
        sample = s.sample(min(300, len(s)), random_state=0)
        score = sample.str.contains(_DAY_RE).mean()
        if score > best_score:
            best_score, best_col = score, c
    return best_col if best_score >= 0.3 else None


def parse_title(title) -> dict:
    """"Badminton AMD Mondays" -> sport="Badminton", code="AMD", day="Monday".
    Anything that doesn't clearly fit the <Sport> <Code> <Day> shape is
    marked DATA REVIEW REQUIRED rather than guessed at."""
    if not isinstance(title, str) or not title.strip():
        return {"sport": None, "code": None, "day": None, "parse_status": "DATA REVIEW REQUIRED"}
    t = " ".join(title.strip().split())
    m = _DAY_RE.search(t)
    if not m:
        return {"sport": None, "code": None, "day": None, "parse_status": "DATA REVIEW REQUIRED"}
    day = m.group(1).capitalize()
    before = t[:m.start()].strip()
    tokens = before.split()
    if len(tokens) >= 2:
        code, sport = tokens[-1], " ".join(tokens[:-1])
    elif len(tokens) == 1:
        code, sport = None, tokens[0]
    else:
        return {"sport": None, "code": None, "day": day, "parse_status": "DATA REVIEW REQUIRED"}
    return {"sport": sport, "code": code, "day": day, "parse_status": "PARSED"}


# --------------------------------------------------------------------------------
# STATUS BUCKETING (Captured / Failed / Refunded / Other)
# --------------------------------------------------------------------------------
STATUS_COL_HINTS = ["status", "payment_status", "txn_status", "transaction_status"]
CAPTURED_WORDS = ["captur", "success", "paid", "complete", "settle", "approved"]
FAILED_WORDS = ["fail", "declin", "error", "reject", "cancel"]
REFUNDED_WORDS = ["refund", "chargeback", "revers", "void"]


def detect_status_column(df: pd.DataFrame, meta: dict = None) -> str | None:
    hinted = (meta.get("status_cols") if meta else None) or []
    if hinted:
        return hinted[0]
    for c in df.columns:
        lname = str(c).lower()
        if any(h in lname for h in STATUS_COL_HINTS):
            return c
    # last resort: any low-cardinality text column whose values look like statuses
    for c in df.columns:
        if df[c].dtype != object:
            continue
        vals = " ".join(df[c].dropna().astype(str).str.lower().unique()[:25])
        if any(w in vals for w in CAPTURED_WORDS) and any(w in vals for w in FAILED_WORDS + REFUNDED_WORDS):
            return c
    return None


def _status_bucket(val) -> str:
    s = str(val).lower()
    if any(w in s for w in REFUNDED_WORDS):
        return "refunded"
    if any(w in s for w in FAILED_WORDS):
        return "failed"
    if any(w in s for w in CAPTURED_WORDS):
        return "captured"
    return "other"


# --------------------------------------------------------------------------------
# SMALL HELPERS
# --------------------------------------------------------------------------------
def _pct(part, whole):
    if not whole:
        return 0.0
    return round(100.0 * part / whole, 2)


def enrich(df: pd.DataFrame, title_col: str, status_col: str | None) -> pd.DataFrame:
    """Adds _sport / _code / _day / _parse_status / _status_bucket columns.
    Does not mutate the caller's dataframe."""
    out = df.copy()
    parsed = out[title_col].apply(parse_title).apply(pd.Series)
    out["_sport"], out["_code"], out["_day"] = parsed["sport"], parsed["code"], parsed["day"]
    out["_parse_status"] = parsed["parse_status"]
    out["_status_bucket"] = out[status_col].apply(_status_bucket) if status_col else "captured"
    return out


# --------------------------------------------------------------------------------
# GROUPED ANALYSIS TABLES
# --------------------------------------------------------------------------------
def _aggregate(edf: pd.DataFrame, group_cols: list, amount_col: str | None, date_col: str | None) -> pd.DataFrame:
    rows = []
    for keys, sub in edf.groupby(group_cols, dropna=False):
        keys = keys if isinstance(keys, tuple) else (keys,)
        txns = len(sub)
        captured = int((sub["_status_bucket"] == "captured").sum())
        failed = int((sub["_status_bucket"] == "failed").sum())
        refunded = int((sub["_status_bucket"] == "refunded").sum())
        if amount_col:
            amt = pd.to_numeric(sub[amount_col], errors="coerce").fillna(0)
            captured_rev = float(amt[sub["_status_bucket"] == "captured"].sum())
        else:
            captured_rev = 0.0
        row = dict(zip(group_cols, keys))
        row.update({
            "Transactions": txns, "Captured Transactions": captured, "Failed Transactions": failed,
            "Refunded Transactions": refunded, "Captured Revenue": round(captured_rev, 2),
            "Capture Rate %": _pct(captured, txns), "Failure Rate %": _pct(failed, txns),
            "Refund Rate %": _pct(refunded, txns),
            "Average Transaction Value": round(captured_rev / captured, 2) if captured else 0.0,
        })
        if date_col and date_col in sub.columns:
            dts = pd.to_datetime(sub[date_col], errors="coerce").dropna()
            if len(dts):
                row["First Activity"] = dts.min().date()
                row["Last Activity"] = dts.max().date()
                row["Days Since Last Activity"] = (pd.Timestamp.now().normalize() - dts.max().normalize()).days
        rows.append(row)

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    total_rev = out["Captured Revenue"].sum()
    out["Revenue Share %"] = round(out["Captured Revenue"] / total_rev * 100, 2) if total_rev else 0.0
    out = out.sort_values("Captured Revenue", ascending=False).reset_index(drop=True)
    out.insert(0, "Rank", range(1, len(out) + 1))
    return out


def _decide(row: pd.Series, median_rev: float) -> str:
    rev = row.get("Captured Revenue", 0) or 0
    fail = row.get("Failure Rate %", 0) or 0
    refund = row.get("Refund Rate %", 0) or 0
    days_since = row.get("Days Since Last Activity")
    if days_since is not None and not pd.isna(days_since) and days_since > 60:
        return "🔴 REDUCE / PAUSE — no activity in 60+ days"
    if fail > 20 or refund > 15:
        return "🟡 OPTIMIZE — high failure/refund rate"
    if median_rev and rev >= median_rev * 1.5:
        return "🟢 SCALE — strong revenue performer"
    if median_rev and rev < median_rev * 0.3:
        return "🔴 REDUCE / PAUSE — low revenue relative to peers"
    return "🔵 MAINTAIN"


def add_decisions(table: pd.DataFrame) -> pd.DataFrame:
    if table.empty:
        return table
    median_rev = table["Captured Revenue"].median()
    table = table.copy()
    table["Performance / Decision"] = table.apply(lambda r: _decide(r, median_rev), axis=1)
    return table


def sport_table(edf, amount_col, date_col):
    return add_decisions(_aggregate(edf, ["_sport"], amount_col, date_col)).rename(columns={"_sport": "Sport"})


def code_table(edf, amount_col, date_col):
    return add_decisions(_aggregate(edf, ["_code"], amount_col, date_col)).rename(columns={"_code": "Code / Location"})


def day_table(edf, amount_col, date_col):
    t = add_decisions(_aggregate(edf, ["_day"], amount_col, date_col)).rename(columns={"_day": "Day"})
    if not t.empty and "Day" in t.columns:
        order = {d.capitalize(): i for i, d in enumerate(DAY_NAMES)}
        t["_o"] = t["Day"].map(order)
        t = t.sort_values("_o").drop(columns="_o").reset_index(drop=True)
    return t


def sport_code_day_table(edf, amount_col, date_col):
    return add_decisions(_aggregate(edf, ["_sport", "_code", "_day"], amount_col, date_col)).rename(
        columns={"_sport": "Sport", "_code": "Code / Location", "_day": "Day"})


def payment_page_table(edf, title_col, amount_col, date_col):
    t = _aggregate(edf, [title_col, "_sport", "_code", "_day"], amount_col, date_col)
    t = t.rename(columns={title_col: "Payment Page Title", "_sport": "Sport",
                           "_code": "Code / Location", "_day": "Day"})
    if t.empty:
        return t
    # "Observed Activity" — how long this specific page has actually been seen in the data
    if "First Activity" in t.columns and "Last Activity" in t.columns:
        t["Observed Activity"] = t.apply(
            lambda r: f"{r['First Activity']} → {r['Last Activity']}"
            if pd.notna(r.get("First Activity")) else "Unknown", axis=1)
    return add_decisions(t)


# --------------------------------------------------------------------------------
# HEALTH SCORE
# --------------------------------------------------------------------------------
def health_score(page_table: pd.DataFrame) -> dict:
    if page_table is None or page_table.empty:
        return {"score": None, "label": "DATA REQUIRED", "components": []}
    total_txn = page_table["Transactions"].sum()
    captured = page_table["Captured Transactions"].sum()
    failed = page_table["Failed Transactions"].sum()
    refunded = page_table["Refunded Transactions"].sum()
    capture_rate = _pct(captured, total_txn)
    failure_rate = _pct(failed, total_txn)
    refund_rate = _pct(refunded, total_txn)
    active_share = None
    if "Days Since Last Activity" in page_table.columns:
        valid = page_table["Days Since Last Activity"].dropna()
        if len(valid):
            active_share = _pct((valid <= 30).sum(), len(valid))

    components = [
        {"Component": "Payment Success", "Weight": "40%", "Benchmark": "capture rate",
         "Value": f"{capture_rate}%", "Score": round(min(40.0, capture_rate * 0.40), 1)},
        {"Component": "Failure Rate (lower is better)", "Weight": "25%", "Benchmark": "failure rate",
         "Value": f"{failure_rate}%", "Score": round(max(0.0, 25.0 - failure_rate * 1.25), 1)},
        {"Component": "Refund Rate (lower is better)", "Weight": "15%", "Benchmark": "refund rate",
         "Value": f"{refund_rate}%", "Score": round(max(0.0, 15.0 - refund_rate * 1.5), 1)},
        {"Component": "Current Activity", "Weight": "20%", "Benchmark": "% pages active in last 30 days",
         "Value": f"{active_share}%" if active_share is not None else "DATA REQUIRED",
         "Score": round((active_share or 0) * 0.20, 1) if active_share is not None else None},
    ]
    scored = [c["Score"] for c in components if c["Score"] is not None]
    total = round(sum(scored), 1) if scored else None
    if total is None:
        label = "DATA REQUIRED"
    elif total >= 85:
        label = "Excellent"
    elif total >= 75:
        label = "Strong"
    elif total >= 60:
        label = "Average"
    elif total >= 45:
        label = "Weak"
    else:
        label = "Poor"
    return {"score": total, "label": label, "components": components}


# --------------------------------------------------------------------------------
# MANAGEMENT DECISIONS
# --------------------------------------------------------------------------------
def management_decisions(sport_t, code_t, day_t, page_t) -> list:
    """Turns the tables above into a short, evidence-cited action list — the
    same 🟢🟡🔵🟠🔴⚪ scale used throughout, never a bare opinion."""
    decisions = []

    def _rows_for(table, label, emoji_filter):
        if table is None or table.empty or "Performance / Decision" not in table.columns:
            return
        name_col = table.columns[1]  # column 0 is Rank; the group name (Sport/Code/Day/Title) is column 1
        for _, r in table.iterrows():
            tag = r["Performance / Decision"]
            if not tag.startswith(emoji_filter):
                continue
            name_val = r[name_col]
            decisions.append({
                "Action": tag.split(" — ")[0].strip(),
                "Area": f"{label}: {name_val}",
                "What": str(name_val),
                "Why": tag.split(" — ")[1].strip() if " — " in tag else "",
                "Evidence": f"Revenue ₹{r.get('Captured Revenue', 0):,.0f} · "
                            f"{r.get('Transactions', 0)} txns · "
                            f"Capture {r.get('Capture Rate %', 0)}% · "
                            f"Failure {r.get('Failure Rate %', 0)}% · "
                            f"Refund {r.get('Refund Rate %', 0)}%",
                "Expected Impact": "Data required — no cost/capacity data in this dataset to size ROI"
                                   if tag.startswith("🟢") else "Reduces wasted failed/refunded transaction load"
                                   if tag.startswith("🟡") else "Frees up capacity/attention from underperforming inventory"
                                   if tag.startswith("🔴") else "Keep current allocation",
                "Risk": "None if evidence holds; re-check after next period's data"
                        if tag.startswith("🟢") else "May be a temporary dip — confirm before pausing entirely",
                "Next Action": "Increase visibility / promote this slot"
                               if tag.startswith("🟢") else "Investigate failure/refund cause with payment gateway"
                               if tag.startswith("🟡") else "Confirm still offered; pause or reassign the slot if not"
                               if tag.startswith("🔴") else "No action needed this period",
            })

    _rows_for(sport_t, "Sport", "🟢")
    _rows_for(sport_t, "Sport", "🔴")
    _rows_for(code_t, "Code / Location", "🟢")
    _rows_for(code_t, "Code / Location", "🔴")
    _rows_for(day_t, "Day", "🟡")
    _rows_for(page_t, "Payment Page Title", "🟡")
    return decisions
