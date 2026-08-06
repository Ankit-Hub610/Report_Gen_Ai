"""
measures.py
-----------
Power-BI style building blocks used by the Custom Builder:

  1. MEASURES        -> the list of aggregations a user can pick for any column
                         (Sum, Average, Count, Distinct Count, Min, Max, Median, Std Dev)
  2. column_kind()    -> classifies ANY column (numeric / date / boolean / categorical / text)
                         so the right filter widget + the right measures are offered for it
  3. list_all_columns()-> every single column in the dataframe, tagged with its kind
                         (this is what was missing before - old filter panel skipped
                         id/text/name columns; here nothing is left out)
  4. compute_measure() -> apply one {column, measure} pair to a (already filtered) dataframe
  5. FilterSpec + apply_filters() -> a generic, per-card / per-chart filter engine that
                         supports both "Basic" (dropdown / slider, like before) and
                         "Advanced" (operator based: contains, >, between, is blank, ...)
                         filtering on ANY column type - this is the same filter model
                         used everywhere (KPI cards, charts, tables) so behaviour is
                         100% consistent across the app.
"""

import re

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------------
# MEASURES
# --------------------------------------------------------------------------------
# GLOBAL measure list - every picker in the app (KPI cards, charts, Data Table,
# future Pivot builder) uses this SAME list, on ANY column, regardless of its
# type. compute_measure() below degrades gracefully ("-") for combos that don't
# make sense (e.g. Sum on a text column) instead of hiding the option.
NUMERIC_MEASURES = ["Sum", "Average", "Count", "Distinct Count", "Min", "Max", "Median", "Std Dev"]
NON_NUMERIC_MEASURES = ["Count", "Distinct Count", "Count Blank", "Most Frequent (Mode)"]
DATE_MEASURES = ["Count", "Distinct Count", "Earliest (Min)", "Latest (Max)"]

ALL_MEASURES = [
    "Sum", "Average", "Count", "Distinct Count", "Min", "Max", "Median", "Std Dev",
    "Count Blank", "Most Frequent (Mode)", "Earliest (Min)", "Latest (Max)",
]

MEASURE_ICONS = {
    "Sum": "Σ", "Average": "x̄", "Count": "#", "Distinct Count": "#!",
    "Min": "↓", "Max": "↑", "Median": "◇", "Std Dev": "σ",
    "Count Blank": "∅", "Most Frequent (Mode)": "★",
    "Earliest (Min)": "↓", "Latest (Max)": "↑",
}


def measures_for_kind(kind: str = None):
    """Global measure list - same options everywhere, on every column type.
    `kind` is kept as a parameter for backward compatibility with existing
    call sites, but is no longer used to shrink the list."""
    return ALL_MEASURES


def column_kind(df: pd.DataFrame, col: str) -> str:
    """Classify a column as numeric / date / boolean / categorical / text.
    Unlike the old profiler, this covers EVERY column - IDs, names, free text,
    everything - so nothing is left out of the filter/measure pickers."""
    if col not in df.columns:
        return "text"
    s = df[col]
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_datetime64_any_dtype(s):
        return "date"
    if pd.api.types.is_numeric_dtype(s):
        return "numeric"
    nunique = s.nunique(dropna=True)
    n = max(len(s), 1)
    if nunique <= 60 or nunique <= 0.5 * n:
        return "categorical"
    return "text"


def list_all_columns(df: pd.DataFrame):
    """Every column in the dataframe, tagged with its kind. Used to populate the
    'Column' picker for KPI cards, X/Y fields and the filter builder - nothing skipped."""
    return [{"name": c, "kind": column_kind(df, c)} for c in df.columns]


def compute_measure(df: pd.DataFrame, col: str, measure: str):
    """Apply one measure to one column of an (already-filtered) dataframe. Returns
    (value, sub_label) where value may be numeric or a string (for Mode/Min-Max date).
    Every measure is available on every column kind now (global measures); if a
    measure genuinely doesn't apply to the data (e.g. Sum on a text column with
    no numeric-looking values), it returns "-" instead of hiding the option."""
    if col not in df.columns or df.empty:
        return None, "no data"
    s = df[col]

    if measure == "Count":
        return int(s.notna().sum()), "non-blank values"
    if measure == "Count Blank":
        return int(s.isna().sum()), "blank values"
    if measure == "Distinct Count":
        return int(s.nunique(dropna=True)), "distinct values"
    if measure == "Most Frequent (Mode)":
        vc = s.dropna().astype(str).value_counts()
        return (vc.index[0] if len(vc) else "-"), "most frequent value"
    if measure == "Earliest (Min)":
        d = pd.to_datetime(s, errors="coerce").dropna()
        return (d.min().date() if len(d) else "-"), "earliest date"
    if measure == "Latest (Max)":
        d = pd.to_datetime(s, errors="coerce").dropna()
        return (d.max().date() if len(d) else "-"), "latest date"

    num = pd.to_numeric(s, errors="coerce")
    has_numeric = num.notna().any()
    if measure == "Sum":
        return (float(num.sum()) if has_numeric else "-"), "sum"
    if measure == "Average":
        return (float(num.mean()) if has_numeric else "-"), "average"
    if measure == "Min":
        return (float(num.min()) if has_numeric else "-"), "minimum"
    if measure == "Max":
        return (float(num.max()) if has_numeric else "-"), "maximum"
    if measure == "Median":
        return (float(num.median()) if has_numeric else "-"), "median"
    if measure == "Std Dev":
        return (float(num.std()) if has_numeric else "-"), "standard deviation"

    return int(len(df)), "rows"


def fmt_measure_value(value):
    """Human friendly formatting: Cr / L / K for big numbers, otherwise plain."""
    if value is None:
        return "-"
    if isinstance(value, (str,)):
        return value
    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if np.isnan(x):
        return "-"
    ax = abs(x)
    if ax >= 1e7:
        return f"{x/1e7:,.2f} Cr"
    if ax >= 1e5:
        return f"{x/1e5:,.2f} L"
    if ax >= 1e3:
        return f"{x/1e3:,.1f} K"
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


# --------------------------------------------------------------------------------
# EXCEL-STYLE NUMBER FORMATTING (used by Custom KPI cards)
# --------------------------------------------------------------------------------
# A small library of presets, familiar from Excel's "Format Cells" dialog, plus
# a "Custom" option where the user can type an Excel-style format code directly
# (e.g. "#,##0.00", "$#,##0", "0.00%", "#,##0,, \"M\""). Only a pragmatic subset
# of Excel's format-code language is supported - enough for the common business
# cases (thousands separator, fixed decimals, currency symbol, percentage,
# scaling by thousands/millions with trailing commas) - not the full spec
# (no colour sections, no date/time codes, no conditional brackets).
NUMBER_FORMAT_PRESETS = {
    "Auto (Cr / L / K)": "auto",
    "General": "general",
    "Number — 1,234": "#,##0",
    "Number — 1,234.00": "#,##0.00",
    "Currency — ₹1,234": "₹#,##0",
    "Currency — ₹1,234.00": "₹#,##0.00",
    "Currency — $1,234": "$#,##0",
    "Currency — $1,234.00": "$#,##0.00",
    "Percentage — 12%": "0%",
    "Percentage — 12.34%": "0.00%",
    "Thousands — 1.2K": "#,##0,\"K\"",
    "Millions — 1.2M": "#,##0.0,,\"M\"",
    "Custom (type Excel format code)": "custom",
}


def format_value(value, format_code: str, custom_code: str = ""):
    """Formats a numeric measure value using an Excel-style format code.
    `format_code` is one of the values in NUMBER_FORMAT_PRESETS (or "auto"/
    "general"/"custom"). For "custom", `custom_code` holds the raw Excel
    format string typed by the user (e.g. "#,##0.00")."""
    if value is None:
        return "-"
    if isinstance(value, str):
        return value  # non-numeric measures (Mode, Min/Max date, ...) pass through untouched

    try:
        x = float(value)
    except (TypeError, ValueError):
        return str(value)
    if np.isnan(x):
        return "-"

    code = format_code or "auto"
    if code == "auto":
        return fmt_measure_value(x)
    if code == "general":
        return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"
    if code == "custom":
        return _apply_excel_format_code(x, custom_code or "#,##0.00")
    return _apply_excel_format_code(x, code)


def _apply_excel_format_code(x: float, code: str) -> str:
    """Interprets a (subset of) Excel number-format code against a float."""
    code = (code or "").strip()
    if not code:
        return f"{x:,.2f}"

    # Percentage - a trailing '%' means multiply by 100
    is_pct = code.endswith("%")
    work_code = code[:-1] if is_pct else code
    val = x * 100 if is_pct else x

    # Scaling commas: each trailing ',' right before the closing quote/suffix
    # divides the value by 1000 (Excel's "thousands scaling" trick), e.g.
    # "#,##0," -> thousands, "#,##0,," -> millions.
    trailing_commas = 0
    stripped = work_code
    # count commas that sit at the very end (ignoring a trailing quoted suffix)
    m = re.match(r'^(.*?)((?:,)+)("\s*[^"]*"\s*)?$', work_code)
    if m and m.group(2):
        trailing_commas = len(m.group(2))
        stripped = m.group(1) + (m.group(3) or "")
    for _ in range(trailing_commas):
        val = val / 1000.0
    work_code = stripped

    # Currency / literal prefix or suffix symbols (₹, $, €, £) kept as-is
    prefix = ""
    for sym in ("₹", "$", "€", "£"):
        if work_code.startswith(sym):
            prefix = sym
            work_code = work_code[len(sym):]
            break

    # Literal quoted suffix, e.g. "K" / "M" in #,##0,"K"
    suffix = ""
    qm = re.search(r'"([^"]*)"', work_code)
    if qm:
        suffix = qm.group(1)
        work_code = work_code[:qm.start()] + work_code[qm.end():]

    use_thousands = "," in work_code
    decimals = 0
    if "." in work_code:
        decimals = len(work_code.split(".", 1)[1].replace("#", "0").rstrip("0#")) or len(work_code.split(".", 1)[1])
        # count of 0/# characters after the decimal point
        frac_part = work_code.split(".", 1)[1]
        decimals = sum(1 for ch in frac_part if ch in "0#")

    try:
        if use_thousands:
            body = f"{val:,.{decimals}f}"
        else:
            body = f"{val:.{decimals}f}"
    except (ValueError, TypeError):
        body = str(val)

    out = f"{prefix}{body}{suffix}"
    if is_pct:
        out += "%"
    return out


# --------------------------------------------------------------------------------
# GENERIC FILTER ENGINE (used by KPI cards, charts, and any future widget)
# --------------------------------------------------------------------------------
OPERATORS_BY_KIND = {
    "categorical": ["is any of", "is none of", "contains", "does not contain", "is blank", "is not blank"],
    "text": ["contains", "does not contain", "starts with", "ends with", "is blank", "is not blank",
              "is any of", "is none of"],
    "numeric": ["=", "≠", ">", ">=", "<", "<=", "between", "is blank", "is not blank"],
    "date": ["on", "before", "after", "between", "in last N days", "is blank", "is not blank"],
    "boolean": ["is true", "is false"],
}


def apply_one_filter(df: pd.DataFrame, f: dict) -> pd.DataFrame:
    """Apply a single filter spec to a dataframe and return the filtered dataframe.
    f = {"column", "kind", "mode": "basic"/"advanced", "operator", "value", "value2"}"""
    col = f.get("column")
    if not col or col not in df.columns:
        return df
    kind = f.get("kind") or column_kind(df, col)
    mode = f.get("mode", "basic")
    s = df[col]

    # ---- BASIC MODE (dropdown / slider - fast, no operator picking) ----
    if mode == "basic":
        if kind in ("categorical", "text", "boolean"):
            vals = f.get("values")
            if vals:
                return df[s.astype(str).isin([str(v) for v in vals])]
            return df
        if kind == "numeric":
            rng = f.get("range")
            if rng:
                num = pd.to_numeric(s, errors="coerce")
                return df[(num >= rng[0]) & (num <= rng[1]) | num.isna()]
            return df
        if kind == "date":
            rng = f.get("range")
            if rng and isinstance(rng, (tuple, list)) and len(rng) == 2:
                d = pd.to_datetime(s, errors="coerce")
                return df[(d.dt.date >= rng[0]) & (d.dt.date <= rng[1]) | d.isna()]
            return df
        return df

    # ---- ADVANCED MODE (operator based, Power-BI style) ----
    op = f.get("operator")
    val = f.get("value")
    val2 = f.get("value2")

    if op == "is blank":
        return df[s.isna() | (s.astype(str).str.strip() == "")]
    if op == "is not blank":
        return df[~(s.isna() | (s.astype(str).str.strip() == ""))]

    if kind in ("categorical", "text"):
        s_str = s.astype(str)
        if op == "is any of" and val:
            return df[s_str.isin([str(v) for v in val])]
        if op == "is none of" and val:
            return df[~s_str.isin([str(v) for v in val])]
        if op == "contains" and val:
            return df[s_str.str.contains(str(val), case=False, na=False)]
        if op == "does not contain" and val:
            return df[~s_str.str.contains(str(val), case=False, na=False)]
        if op == "starts with" and val:
            return df[s_str.str.startswith(str(val), na=False)]
        if op == "ends with" and val:
            return df[s_str.str.endswith(str(val), na=False)]
        return df

    if kind == "numeric":
        num = pd.to_numeric(s, errors="coerce")
        try:
            if op == "=" and val not in (None, ""):
                return df[num == float(val)]
            if op == "≠" and val not in (None, ""):
                return df[num != float(val)]
            if op == ">" and val not in (None, ""):
                return df[num > float(val)]
            if op == ">=" and val not in (None, ""):
                return df[num >= float(val)]
            if op == "<" and val not in (None, ""):
                return df[num < float(val)]
            if op == "<=" and val not in (None, ""):
                return df[num <= float(val)]
            if op == "between" and val not in (None, "") and val2 not in (None, ""):
                return df[(num >= float(val)) & (num <= float(val2))]
        except (TypeError, ValueError):
            return df
        return df

    if kind == "date":
        d = pd.to_datetime(s, errors="coerce")
        try:
            if op == "on" and val:
                target = pd.to_datetime(val).date()
                return df[d.dt.date == target]
            if op == "before" and val:
                return df[d < pd.to_datetime(val)]
            if op == "after" and val:
                return df[d > pd.to_datetime(val)]
            if op == "between" and val and val2:
                return df[(d >= pd.to_datetime(val)) & (d <= pd.to_datetime(val2))]
            if op == "in last N days" and val not in (None, ""):
                cutoff = pd.Timestamp.today() - pd.Timedelta(days=float(val))
                return df[d >= cutoff]
        except (TypeError, ValueError):
            return df
        return df

    if kind == "boolean":
        if op == "is true":
            return df[s == True]  # noqa: E712
        if op == "is false":
            return df[s == False]  # noqa: E712
        return df

    return df


def apply_filters(df: pd.DataFrame, filters: list) -> pd.DataFrame:
    """Apply a list of filter specs in sequence (AND logic, like Power BI's filter pane)."""
    out = df
    for f in filters or []:
        try:
            out = apply_one_filter(out, f)
        except Exception:
            continue
    return out


def describe_filter(f: dict) -> str:
    """Small human-readable chip label for a filter, e.g. 'status is any of captured, failed'."""
    col = f.get("column", "?")
    if f.get("mode", "basic") == "basic":
        if "values" in f and f["values"]:
            return f"{col}: {', '.join(str(v) for v in f['values'][:3])}" + (" ..." if len(f["values"]) > 3 else "")
        rng = f.get("range")
        if rng:
            # A date_input widget can momentarily return a single date (only the
            # start of the range picked so far) instead of a 2-tuple - guard
            # against that here so a half-picked range never crashes the chip.
            if isinstance(rng, (tuple, list)) and len(rng) == 2:
                lo, hi = rng
                return f"{col}: {lo} → {hi}"
            if isinstance(rng, (tuple, list)) and len(rng) == 1:
                return f"{col}: {rng[0]} → (pick end date)"
            return f"{col}: {rng} → (pick end date)"
        return col
    op = f.get("operator", "")
    val = f.get("value", "")
    val2 = f.get("value2", "")
    if op == "between":
        return f"{col} {op} {val} and {val2}"
    if op in ("is blank", "is not blank", "is true", "is false"):
        return f"{col} {op}"
    return f"{col} {op} {val}".strip()
