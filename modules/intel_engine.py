"""
intel_engine.py
----------------
Deterministic analytics engine behind the "🧠 Intelligence Report" page.

Everything a NUMBER in the final report is calculated here in plain pandas/
numpy — never by the AI. The AI (see ai_chat.generate_report_narrative) is
only ever handed the numbers already computed here and asked to explain /
prioritise / write them up in business language — it is never allowed to
invent a figure. This mirrors the "NEVER invent data" rule from the master
analytics prompt this page implements.

Works on ANY dataset (same philosophy as data_engine.py) — column ROLES
(revenue, cost, customer, product, location, date) are guessed from
meta/profile_columns() + column-name hints, then confirmed/overridden by the
user in the UI before anything is calculated.
"""

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------
# ROLE DETECTION
# --------------------------------------------------------------------------------
REVENUE_HINTS = ["revenue", "sales", "amount", "total", "price", "payment", "income", "value", "turnover"]
COST_HINTS = ["cost", "expense", "expenditure", "spend", "cogs"]
PROFIT_HINTS = ["profit", "margin", "net income", "earnings"]
CUSTOMER_HINTS = ["customer", "client", "buyer", "account", "member", "player", "user"]
PRODUCT_HINTS = ["product", "item", "service", "sku", "category", "plan"]
LOCATION_HINTS = ["location", "city", "state", "region", "country", "market", "branch", "store", "territory"]
ORDER_HINTS = ["order", "transaction", "invoice", "bill", "receipt"]
CHANNEL_HINTS = ["channel", "source", "platform", "medium"]


def _best_match(candidates, hints, prefer_numeric=None, df=None):
    scored = []
    for c in candidates:
        lname = str(c).lower()
        score = sum(1 for h in hints if h in lname)
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    return scored[0][1] if scored else None


def detect_roles(df: pd.DataFrame, meta: dict) -> dict:
    """Best-guess column roles. Returns a flat dict — every value is either a
    real column name from df, or None if nothing plausible was found."""
    numeric = meta.get("numeric_cols", [])
    categorical = meta.get("categorical_cols", [])
    id_like = meta.get("id_like_cols", [])
    name_like = meta.get("name_like_cols", [])
    all_cols = list(df.columns)

    revenue = _best_match(numeric, REVENUE_HINTS) or (numeric[0] if numeric else None)
    cost = _best_match([c for c in numeric if c != revenue], COST_HINTS)
    profit = _best_match([c for c in numeric if c not in (revenue, cost)], PROFIT_HINTS)
    customer = _best_match(categorical + id_like + name_like, CUSTOMER_HINTS)
    product = _best_match(categorical, PRODUCT_HINTS)
    location = _best_match(categorical, LOCATION_HINTS)
    channel = _best_match(categorical, CHANNEL_HINTS)
    order_id = _best_match(id_like + all_cols, ORDER_HINTS)
    date = meta.get("primary_date")

    return {
        "revenue": revenue, "cost": cost, "profit": profit,
        "customer": customer, "product": product, "location": location,
        "channel": channel, "order_id": order_id, "date": date,
    }


# --------------------------------------------------------------------------------
# SMALL HELPERS
# --------------------------------------------------------------------------------
def _num(s):
    return pd.to_numeric(s, errors="coerce")


def _pct(part, whole):
    if whole in (0, None) or (isinstance(whole, float) and (np.isnan(whole) or whole == 0)):
        return None
    return 100.0 * part / whole


def _safe_round(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), nd)


# --------------------------------------------------------------------------------
# DATA QUALITY
# --------------------------------------------------------------------------------
def data_quality_score(df: pd.DataFrame, meta: dict) -> dict:
    issues = []
    n_rows, n_cols = len(df), len(df.columns)
    penalty = 0

    missing_pct = 100 * df.isna().sum().sum() / max(1, n_rows * n_cols)
    if missing_pct > 1:
        penalty += min(25, missing_pct * 0.6)
        issues.append(f"{missing_pct:.1f}% of all cells are missing/blank.")

    dup_count = int(df.duplicated().sum())
    if dup_count > 0:
        dup_pct = 100 * dup_count / max(1, n_rows)
        penalty += min(20, dup_pct * 0.8)
        issues.append(f"{dup_count:,} fully duplicate rows found ({dup_pct:.1f}% of rows).")

    for col in meta.get("numeric_cols", []):
        s = _num(df[col])
        if s.notna().sum() == 0:
            continue
        neg_count = int((s < 0).sum())
        lname = col.lower()
        if neg_count > 0 and not any(h in lname for h in ["profit", "loss", "change", "delta", "growth", "diff"]):
            penalty += min(5, 100 * neg_count / max(1, n_rows) * 0.3)
            issues.append(f"'{col}' has {neg_count:,} negative value(s) — check if that's expected.")

    for col in meta.get("date_cols", []):
        s = pd.to_datetime(df[col], errors="coerce")
        bad = int(s.isna().sum() - df[col].isna().sum())
        if bad > 0:
            penalty += min(5, 100 * bad / max(1, n_rows) * 0.3)
            issues.append(f"'{col}' has {bad:,} value(s) that don't parse as valid dates.")

    score = max(0, round(100 - penalty))
    if not issues:
        issues.append("No major data-quality problems detected.")
    return {"score": score, "issues": issues, "duplicate_rows": dup_count, "missing_pct": round(missing_pct, 2)}


# --------------------------------------------------------------------------------
# FINANCIAL / KPI SUMMARY
# --------------------------------------------------------------------------------
def compute_financials(df: pd.DataFrame, roles: dict) -> dict:
    out = {"profit_calculable": False}
    rev_col, cost_col, profit_col = roles.get("revenue"), roles.get("cost"), roles.get("profit")

    revenue = _num(df[rev_col]).sum() if rev_col else None
    out["total_revenue"] = _safe_round(revenue)
    out["revenue_col"] = rev_col

    if profit_col:
        profit = _num(df[profit_col]).sum()
        out["total_profit"] = _safe_round(profit)
        out["profit_calculable"] = True
    elif rev_col and cost_col:
        cost = _num(df[cost_col]).sum()
        profit = (revenue or 0) - cost
        out["total_cost"] = _safe_round(cost)
        out["total_profit"] = _safe_round(profit)
        out["profit_calculable"] = True
    else:
        out["total_cost"] = _safe_round(_num(df[cost_col]).sum()) if cost_col else None
        out["total_profit"] = None

    if out["profit_calculable"] and revenue:
        out["profit_margin_pct"] = _safe_round(_pct(out["total_profit"], revenue))
    else:
        out["profit_margin_pct"] = None

    order_col = roles.get("order_id")
    if order_col:
        out["total_orders"] = int(df[order_col].nunique())
    else:
        out["total_orders"] = int(len(df))

    if rev_col and out["total_orders"]:
        out["avg_order_value"] = _safe_round(revenue / out["total_orders"]) if revenue is not None else None
    else:
        out["avg_order_value"] = None

    cust_col = roles.get("customer")
    if cust_col:
        out["customer_count"] = int(df[cust_col].nunique())
        if rev_col and out["customer_count"]:
            out["revenue_per_customer"] = _safe_round(revenue / out["customer_count"])
    else:
        out["customer_count"] = None
        out["revenue_per_customer"] = None

    return out


# --------------------------------------------------------------------------------
# TIME SERIES: GROWTH, TREND, FORECAST, ANOMALIES
# --------------------------------------------------------------------------------
def _monthly_series(df, date_col, value_col, agg="sum"):
    tmp = df[[date_col, value_col]].copy()
    tmp[date_col] = pd.to_datetime(tmp[date_col], errors="coerce")
    tmp[value_col] = _num(tmp[value_col])
    tmp = tmp.dropna(subset=[date_col, value_col])
    if tmp.empty:
        return pd.Series(dtype=float)
    tmp = tmp.set_index(date_col)
    ser = tmp[value_col].resample("ME").agg(agg)
    return ser


def compute_trend_growth(df: pd.DataFrame, roles: dict) -> dict:
    date_col, rev_col = roles.get("date"), roles.get("revenue")
    out = {"available": False}
    if not date_col or not rev_col:
        out["reason"] = "No date column and/or revenue column available — trend/growth analysis skipped."
        return out

    ser = _monthly_series(df, date_col, rev_col)
    if len(ser) < 2:
        out["reason"] = "Fewer than 2 distinct months of data — not enough history for a trend."
        return out

    out["available"] = True
    out["periods"] = [d.strftime("%b %Y") for d in ser.index]
    out["values"] = [round(float(v), 2) for v in ser.values]
    mom = ser.pct_change() * 100
    out["mom_growth_pct"] = [None if pd.isna(v) else round(float(v), 1) for v in mom.values]

    best_idx = int(np.argmax(ser.values))
    worst_idx = int(np.argmin(ser.values))
    out["best_period"] = out["periods"][best_idx]
    out["best_period_value"] = out["values"][best_idx]
    out["worst_period"] = out["periods"][worst_idx]
    out["worst_period_value"] = out["values"][worst_idx]

    first, last = ser.values[0], ser.values[-1]
    out["overall_change_pct"] = _safe_round(_pct(last - first, first)) if first else None

    n_periods = len(ser)
    if n_periods >= 3:
        years = (ser.index[-1] - ser.index[0]).days / 365.25
        if years > 0 and first > 0:
            cagr = (((last / first) ** (1 / years)) - 1) * 100
            out["cagr_pct"] = _safe_round(cagr)
        else:
            out["cagr_pct"] = None
    else:
        out["cagr_pct"] = None

    return out


def compute_forecast(df: pd.DataFrame, roles: dict, periods_ahead=3) -> dict:
    date_col, rev_col = roles.get("date"), roles.get("revenue")
    out = {"available": False}
    if not date_col or not rev_col:
        out["reason"] = "No date/revenue column — forecast skipped."
        return out

    ser = _monthly_series(df, date_col, rev_col)
    if len(ser) < 4:
        out["reason"] = f"Only {len(ser)} month(s) of history — need at least 4 for a reasonable forecast."
        return out

    y = ser.values.astype(float)
    x = np.arange(len(y))
    coeffs = np.polyfit(x, y, 1)
    slope, intercept = coeffs[0], coeffs[1]
    y_pred = slope * x + intercept
    ss_res = np.sum((y - y_pred) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0

    future_x = np.arange(len(y), len(y) + periods_ahead)
    future_y = slope * future_x + intercept
    future_y = np.clip(future_y, 0, None)  # revenue forecast shouldn't go negative

    last_date = ser.index[-1]
    future_periods = [(last_date + pd.DateOffset(months=i + 1)).strftime("%b %Y") for i in range(periods_ahead)]

    if r2 >= 0.6:
        confidence = "Medium-High"
    elif r2 >= 0.3:
        confidence = "Medium"
    else:
        confidence = "Low"

    out.update({
        "available": True,
        "method": "Linear trend regression on monthly totals",
        "r2": _safe_round(r2, 3),
        "confidence": confidence,
        "direction": "Upward" if slope > 0 else ("Downward" if slope < 0 else "Flat"),
        "forecast_periods": future_periods,
        "forecast_values": [round(float(v), 2) for v in future_y],
        "history_periods": [d.strftime("%b %Y") for d in ser.index],
        "history_values": [round(float(v), 2) for v in ser.values],
    })
    return out


def detect_anomalies(df: pd.DataFrame, roles: dict, z_thresh=2.0) -> list:
    date_col, rev_col = roles.get("date"), roles.get("revenue")
    if not date_col or not rev_col:
        return []
    ser = _monthly_series(df, date_col, rev_col)
    if len(ser) < 4:
        return []
    mean, std = ser.mean(), ser.std()
    if not std or np.isnan(std) or std == 0:
        return []
    anomalies = []
    for dt, val in ser.items():
        z = (val - mean) / std
        if abs(z) >= z_thresh:
            anomalies.append({
                "period": dt.strftime("%b %Y"),
                "value": round(float(val), 2),
                "expected_range": f"{round(float(mean - std), 2)} – {round(float(mean + std), 2)}",
                "z_score": round(float(z), 2),
                "direction": "spike" if z > 0 else "drop",
            })
    return anomalies


# --------------------------------------------------------------------------------
# TOP / BOTTOM BREAKDOWNS + CONCENTRATION
# --------------------------------------------------------------------------------
def top_bottom_by_dimension(df: pd.DataFrame, dim_col, measure_col, n=5) -> dict:
    if not dim_col or not measure_col or dim_col not in df.columns or measure_col not in df.columns:
        return {"available": False}
    tmp = df[[dim_col, measure_col]].copy()
    tmp[measure_col] = _num(tmp[measure_col])
    tmp = tmp.dropna(subset=[dim_col, measure_col])
    if tmp.empty:
        return {"available": False}
    grouped = tmp.groupby(dim_col)[measure_col].sum().sort_values(ascending=False)
    total = grouped.sum()
    top_n = grouped.head(n)
    bottom_n = grouped.tail(n).sort_values()
    top5_share = _pct(grouped.head(5).sum(), total)
    return {
        "available": True,
        "dimension": dim_col,
        "measure": measure_col,
        "top": [{"name": str(k), "value": round(float(v), 2), "share_pct": _safe_round(_pct(v, total))} for k, v in top_n.items()],
        "bottom": [{"name": str(k), "value": round(float(v), 2), "share_pct": _safe_round(_pct(v, total))} for k, v in bottom_n.items()],
        "unique_count": int(grouped.shape[0]),
        "top5_share_pct": _safe_round(top5_share),
    }


# --------------------------------------------------------------------------------
# CORRELATIONS
# --------------------------------------------------------------------------------
def compute_correlations(df: pd.DataFrame, meta: dict, max_pairs=6) -> list:
    numeric_cols = [c for c in meta.get("numeric_cols", []) if df[c].notna().sum() > 2]
    if len(numeric_cols) < 2:
        return []
    corr = df[numeric_cols].corr(numeric_only=True)
    pairs = []
    seen = set()
    for a in numeric_cols:
        for b in numeric_cols:
            if a == b or (b, a) in seen:
                continue
            seen.add((a, b))
            val = corr.loc[a, b]
            if pd.isna(val):
                continue
            av = abs(val)
            if av >= 0.7:
                strength = "Strong"
            elif av >= 0.4:
                strength = "Moderate"
            elif av >= 0.2:
                strength = "Weak"
            else:
                continue  # not meaningful enough to report
            pairs.append({
                "a": a, "b": b, "corr": round(float(val), 2), "strength": strength,
                "direction": "Positive" if val > 0 else "Negative",
            })
    pairs.sort(key=lambda p: -abs(p["corr"]))
    return pairs[:max_pairs]


# --------------------------------------------------------------------------------
# HEALTH BADGE (deterministic — no AI)
# --------------------------------------------------------------------------------
def compute_health(financials: dict, trend: dict, quality: dict) -> dict:
    score = 0
    reasons = []

    q = quality.get("score", 100)
    if q >= 80:
        score += 2
    elif q >= 50:
        score += 1
        reasons.append("data quality has some gaps")
    else:
        reasons.append("data quality is weak — treat findings with caution")

    if financials.get("profit_calculable"):
        margin = financials.get("profit_margin_pct")
        if margin is not None:
            if margin >= 15:
                score += 2
            elif margin >= 5:
                score += 1
                reasons.append("profit margin is thin")
            else:
                reasons.append("profit margin is very low or negative")

    if trend.get("available"):
        chg = trend.get("overall_change_pct")
        if chg is not None:
            if chg > 5:
                score += 2
                reasons.append("revenue trend is positive")
            elif chg >= -5:
                score += 1
                reasons.append("revenue is roughly flat")
            else:
                reasons.append("revenue is declining")

    if score >= 5:
        label = "Healthy"
    elif score >= 3:
        label = "Stable"
    elif score >= 1:
        label = "At Risk"
    else:
        label = "Critical"

    return {"label": label, "score_raw": score, "reasons": reasons}


# --------------------------------------------------------------------------------
# MASTER BUNDLE
# --------------------------------------------------------------------------------
def build_facts_bundle(df: pd.DataFrame, meta: dict, roles: dict) -> dict:
    quality = data_quality_score(df, meta)
    financials = compute_financials(df, roles)
    trend = compute_trend_growth(df, roles)
    forecast = compute_forecast(df, roles)
    anomalies = detect_anomalies(df, roles)
    correlations = compute_correlations(df, meta)
    health = compute_health(financials, trend, quality)

    rev_col = roles.get("revenue")
    breakdowns = {}
    for key in ("product", "customer", "location", "channel"):
        breakdowns[key] = top_bottom_by_dimension(df, roles.get(key), rev_col)

    return {
        "row_count": len(df),
        "col_count": len(df.columns),
        "roles": roles,
        "quality": quality,
        "financials": financials,
        "trend": trend,
        "forecast": forecast,
        "anomalies": anomalies,
        "correlations": correlations,
        "breakdowns": breakdowns,
        "health": health,
    }


def facts_hash(df: pd.DataFrame, roles: dict, language: str) -> str:
    """Cheap fingerprint so the app can avoid recomputation/AI-recall when
    nothing has actually changed."""
    try:
        h = pd.util.hash_pandas_object(df, index=False).sum()
    except Exception:
        h = len(df)
    return f"{h}|{len(df)}|{len(df.columns)}|{roles}|{language}"


# --------------------------------------------------------------------------------
# TURN THE FACTS BUNDLE INTO A COMPACT TEXT PROMPT FOR THE AI NARRATIVE STEP
# --------------------------------------------------------------------------------
def facts_to_prompt_text(facts: dict) -> str:
    lines = []
    lines.append(f"Rows: {facts['row_count']:,} | Columns: {facts['col_count']}")
    roles = facts["roles"]
    lines.append("Detected columns: " + ", ".join(f"{k}={v}" for k, v in roles.items() if v))

    q = facts["quality"]
    lines.append(f"\nDATA QUALITY: score {q['score']}/100. Issues: " + " | ".join(q["issues"]))

    f = facts["financials"]
    lines.append(f"\nFINANCIALS: total_revenue={f.get('total_revenue')}")
    if f.get("profit_calculable"):
        lines.append(f"total_cost={f.get('total_cost')} total_profit={f.get('total_profit')} "
                      f"profit_margin_pct={f.get('profit_margin_pct')}")
    else:
        lines.append("Profit cannot be directly calculated from the available data (no cost/profit column).")
    lines.append(f"total_orders={f.get('total_orders')} avg_order_value={f.get('avg_order_value')} "
                  f"customer_count={f.get('customer_count')} revenue_per_customer={f.get('revenue_per_customer')}")

    t = facts["trend"]
    if t.get("available"):
        lines.append(f"\nTREND (monthly revenue, ACTUAL data): periods={t['periods']} values={t['values']}")
        lines.append(f"best_period={t['best_period']} ({t['best_period_value']}) "
                      f"worst_period={t['worst_period']} ({t['worst_period_value']}) "
                      f"overall_change_pct={t['overall_change_pct']} cagr_pct={t.get('cagr_pct')}")
    else:
        lines.append(f"\nTREND: not available — {t.get('reason')}")

    fc = facts["forecast"]
    if fc.get("available"):
        lines.append(f"\nFORECAST (method: {fc['method']}, R²={fc['r2']}, confidence={fc['confidence']}): "
                      f"direction={fc['direction']} periods={fc['forecast_periods']} values={fc['forecast_values']} "
                      f"— these are ESTIMATES, not actuals.")
    else:
        lines.append(f"\nFORECAST: not available — {fc.get('reason')}")

    if facts["anomalies"]:
        lines.append("\nANOMALIES (monthly revenue outliers):")
        for a in facts["anomalies"]:
            lines.append(f"  - {a['period']}: {a['value']} ({a['direction']}, z={a['z_score']}, "
                         f"expected range {a['expected_range']})")
    else:
        lines.append("\nANOMALIES: none detected above threshold.")

    if facts["correlations"]:
        lines.append("\nCORRELATIONS:")
        for c in facts["correlations"]:
            lines.append(f"  - {c['a']} vs {c['b']}: corr={c['corr']} ({c['strength']}, {c['direction']})")
    else:
        lines.append("\nCORRELATIONS: none strong enough to report.")

    for key, label in (("product", "PRODUCT"), ("customer", "CUSTOMER"), ("location", "LOCATION"), ("channel", "CHANNEL")):
        b = facts["breakdowns"].get(key, {})
        if b.get("available"):
            lines.append(f"\n{label} BREAKDOWN (by {b['measure']}, dimension={b['dimension']}, "
                         f"{b['unique_count']} unique values, top-5 share={b['top5_share_pct']}%):")
            lines.append(f"  Top: {b['top']}")
            lines.append(f"  Bottom: {b['bottom']}")
        else:
            lines.append(f"\n{label} BREAKDOWN: not available (no matching column detected).")

    h = facts["health"]
    lines.append(f"\nDETERMINISTIC HEALTH ASSESSMENT (already computed, do not contradict): {h['label']} "
                 f"— reasons: {h['reasons']}")

    return "\n".join(lines)
