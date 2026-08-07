"""
data_engine.py
--------------
Universal data loading + column-profiling + KPI auto-generation engine.
Works on ANY tabular dataset (sports data, sales data, HR data, anything) -
no column names are hard-coded. Everything is detected dynamically from the
data itself, so the same code path works whether you load the sample sports
payments file or a completely different dataset.
"""

import io
import json
import warnings
import numpy as np
import pandas as pd
import streamlit as st

MAX_CARDINALITY_FOR_CATEGORY = 60   # a text column with more unique values than this is treated as "free text", not a chart dimension
MIN_CARDINALITY_FOR_CATEGORY = 2
ID_HINT_WORDS = ["id", "uuid", "guid", "code_ref", "token", "code", "no.", "number",
                 "serial", "sr_no", "srno", "ref", "index", "pincode", "pin_code",
                 "zipcode", "zip_code", "phone", "mobile", "contact_no"]
# Numeric columns whose NAME matches one of these are "dimension-like" - useful to
# group/filter by, but summing or averaging them produces a meaningless KPI/chart
# (e.g. "Total Year", "Average Jersey Number"). They're kept as a grouping/filter
# dimension (categorical) instead of being treated as an additive measure.
NON_ADDITIVE_HINT_WORDS = ["year", "yr", "age", "rank", "season", "grade", "level",
                           "jersey", "position", "pos", "round", "week", "month",
                           "quarter", "over", "innings", "set", "game_no", "match_no",
                           "match_number", "kit_number"]
MEASURE_HINT_WORDS = ["amount", "revenue", "sales", "price", "total", "payment",
                      "value", "income", "cost", "fee", "qty", "quantity", "count",
                      "score", "rating", "duration", "attendance"]
NAME_HINT_WORDS = ["name", "email", "phone", "customer", "player", "user"]
STATUS_HINT_WORDS = ["status", "stage", "state"]


# --------------------------------------------------------------------------------
# LOADING
# --------------------------------------------------------------------------------
def _find_header_row(buf, sheet_name, scan_rows=15):
    """Real-world Excel exports often have 1-3 title/blank rows above the
    actual header row (a report title, a generated-on date, a blank spacer
    row). If we always take row 0 as the header, those get read as data and
    the real header row gets read as data too — so every column ends up
    named 'Unnamed: 0', 'Unnamed: 1', etc, which then produces nonsense KPI
    cards ("Total Unnamed: 9") and breaks chart generation entirely (there
    are no sensibly-named columns left to build a chart from).

    Scans the first few rows and picks the first one where most cells are
    filled in and look like short text labels — a strong header-row
    signature — rather than assuming row 0.
    """
    try:
        preview = pd.read_excel(buf, sheet_name=sheet_name, header=None, nrows=scan_rows)
    except Exception:
        return 0
    best_row, best_score = 0, -1
    for i in range(len(preview)):
        row = preview.iloc[i]
        non_null = row.notna().sum()
        if non_null < max(2, int(0.5 * len(row))):
            continue
        texty = sum(1 for v in row if isinstance(v, str) and 0 < len(v.strip()) <= 40)
        uniq = row.astype(str).nunique()
        score = non_null + texty + uniq
        if score > best_score:
            best_score, best_row = score, i
    return best_row


def load_dataframe(uploaded_file):
    """Load csv / xlsx / xls / json / pdf into a dict of {sheet_name: DataFrame}."""
    name = uploaded_file.name.lower()
    data = uploaded_file.read()
    buf = io.BytesIO(data)

    if name.endswith(".csv") or name.endswith(".tsv"):
        sep = "\t" if name.endswith(".tsv") else None
        df = pd.read_csv(buf, sep=sep, engine="python")
        return {"Sheet1": df}

    if name.endswith(".xlsx") or name.endswith(".xls"):
        xls = pd.ExcelFile(buf)
        sheets = {}
        for s in xls.sheet_names:
            try:
                buf.seek(0)
                header_row = _find_header_row(buf, s)
                buf.seek(0)
                sheets[s] = pd.read_excel(buf, sheet_name=s, header=header_row)
            except Exception:
                try:
                    sheets[s] = xls.parse(s)   # fall back to the plain read if detection itself errors
                except Exception:
                    pass
        return sheets

    if name.endswith(".json"):
        raw = json.loads(data.decode("utf-8", errors="ignore"))
        if isinstance(raw, dict):
            # could be {"records":[...]} or a dict-of-dicts
            for key in ("data", "records", "rows", "results"):
                if key in raw and isinstance(raw[key], list):
                    return {"Sheet1": pd.json_normalize(raw[key])}
            return {"Sheet1": pd.json_normalize([raw])}
        return {"Sheet1": pd.json_normalize(raw)}

    if name.endswith(".pdf"):
        try:
            import pdfplumber
            frames = []
            with pdfplumber.open(buf) as pdf:
                for page in pdf.pages:
                    for tbl in page.extract_tables():
                        if tbl and len(tbl) > 1:
                            frames.append(pd.DataFrame(tbl[1:], columns=tbl[0]))
            if frames:
                return {f"Table_{i+1}": f for i, f in enumerate(frames)}
        except Exception as e:
            st.error(f"PDF me table nahi mil paaya: {e}")
        return {"Sheet1": pd.DataFrame()}

    raise ValueError("Unsupported file type. Please upload CSV, XLSX, JSON or PDF.")


def combine_dataframes(named_dfs: list, mode: str = "stack", source_col: str = "Source File") -> pd.DataFrame:
    """Combines several (name, DataFrame) pairs into one DataFrame, for the
    'multiple file upload, combined' feature on Raw Analysis.

    mode:
      - "stack"  -> stack rows on top of each other (like appending monthly
                    exports). Columns are unioned - a column missing in one
                    file just comes out blank for those rows. A `source_col`
                    column is added so you can always tell which file a row
                    came from.
      - "columns" -> paste sheets side-by-side (outer-join on the row index) -
                    useful when each file contributes different columns for
                    the SAME set of rows.
    """
    named_dfs = [(n, d) for n, d in named_dfs if d is not None and not d.empty]
    if not named_dfs:
        return pd.DataFrame()
    if len(named_dfs) == 1:
        return named_dfs[0][1]

    if mode == "columns":
        frames = [d.reset_index(drop=True) for _, d in named_dfs]
        combined = pd.concat(frames, axis=1)
        combined.columns = _dedup_columns(combined.columns)  # avoid duplicate column names across files
        return combined

    frames = []
    for name, d in named_dfs:
        d = d.copy()
        d[source_col] = name
        frames.append(d)
    combined = pd.concat(frames, axis=0, ignore_index=True, sort=False)
    # keep the source column last and readable
    cols = [c for c in combined.columns if c != source_col] + [source_col]
    return combined[cols]


def _dedup_columns(columns):
    seen = {}
    out = []
    for c in columns:
        if c not in seen:
            seen[c] = 0
            out.append(c)
        else:
            seen[c] += 1
            out.append(f"{c}_{seen[c]}")
    return out


DATE_NAME_HINTS = ["date", "time", "day", "created", "updated", "timestamp",
                    "dt", "dob", "modified", "expiry", "expire", "due", "period"]


def _best_date_parse(s: pd.Series):
    """Try a few common parse strategies (default, day-first e.g. DD/MM/YYYY,
    and pandas' own mixed-format inference) and return whichever one parses
    the highest fraction of values, as (parsed_series, success_rate). Never
    raises - a strategy that errors out is just skipped."""
    best_parsed, best_rate = None, -1.0
    for kwargs in ({}, {"dayfirst": True}, {"format": "mixed"}, {"format": "mixed", "dayfirst": True}):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                parsed = pd.to_datetime(s, errors="coerce", **kwargs)
        except (TypeError, ValueError):
            continue
        rate = parsed.notna().mean() if len(parsed) else 0
        if rate > best_rate:
            best_parsed, best_rate = parsed, rate
    return best_parsed, max(best_rate, 0)


def _maybe_epoch_dates(s: pd.Series):
    """Numeric columns that are actually Unix timestamps (seconds or
    milliseconds since epoch), recognised only when the values fall in a
    plausible calendar-date range - avoids turning ordinary numeric
    measures/IDs into fake dates."""
    num = pd.to_numeric(s, errors="coerce")
    valid = num.dropna()
    if valid.empty:
        return None
    med = valid.abs().median()
    if 1e11 <= med < 1e13:      # milliseconds since epoch (~2001-2286)
        unit = "ms"
    elif 1e9 <= med < 1e10:     # seconds since epoch (~2001-2286)
        unit = "s"
    else:
        return None
    try:
        parsed = pd.to_datetime(num, unit=unit, errors="coerce")
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed.notna().mean() > 0.9 else None


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Light generic cleanup: strip col names, drop fully-empty rows/cols, try to parse dates."""
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(axis=1, how="all")
    df = df.dropna(axis=0, how="all")

    for col in df.columns:
        lname = col.lower()
        name_hints_date = any(k in lname for k in DATE_NAME_HINTS)

        # NOTE: this used to check `df[col].dtype == object` only. Newer
        # pandas (2.x with the string-dtype option, and 3.x by default) reads
        # text columns from CSV/XLSX as a native "str" dtype instead of the
        # old "object" dtype - that equality check silently missed those
        # columns entirely, so dates (and numeric-looking text) in a text
        # column never got parsed/converted on those pandas versions. Using
        # is_string_dtype() catches both "object" and the newer "str" dtype.
        is_textlike = df[col].dtype == object or pd.api.types.is_string_dtype(df[col])
        if is_textlike and not pd.api.types.is_bool_dtype(df[col]):
            sample = df[col].dropna().astype(str).head(200)
            if len(sample) == 0:
                continue
            # Numeric-looking text (amounts, IDs, ...) should stay numeric,
            # not get accidentally swept into a date parse below.
            numeric_try = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
            is_mostly_numeric = numeric_try.notna().mean() > 0.9 and df[col].notna().sum() > 0

            if not is_mostly_numeric:
                # Try to parse as a date even without a "date"-ish column
                # name (real-world files use all sorts of headers), but
                # require a much higher success rate when the name gives no
                # hint at all, so plain text columns aren't misread as dates.
                parsed, rate = _best_date_parse(df[col])
                threshold = 0.5 if name_hints_date else 0.85
                if parsed is not None and rate > threshold and parsed.nunique(dropna=True) > 1:
                    df[col] = parsed
                    continue

            if is_mostly_numeric:
                df[col] = numeric_try
                continue

        elif name_hints_date and pd.api.types.is_numeric_dtype(df[col]) and not pd.api.types.is_bool_dtype(df[col]):
            # A numeric column whose name suggests a date (e.g. "created_at"
            # stored as a Unix timestamp) - only converted when the values
            # actually look like epoch timestamps.
            epoch_parsed = _maybe_epoch_dates(df[col])
            if epoch_parsed is not None:
                df[col] = epoch_parsed
                continue

    return df.reset_index(drop=True)


# --------------------------------------------------------------------------------
# COLUMN PROFILING
# --------------------------------------------------------------------------------
def profile_columns(df: pd.DataFrame) -> dict:
    n = len(df)
    date_cols, numeric_cols, categorical_cols, id_like_cols, text_cols, bool_cols, status_cols, name_like_cols = (
        [], [], [], [], [], [], [], []
    )

    for col in df.columns:
        s = df[col]
        lname = col.lower()
        if pd.api.types.is_bool_dtype(s):
            bool_cols.append(col)
            categorical_cols.append(col)
            continue
        if pd.api.types.is_datetime64_any_dtype(s):
            date_cols.append(col)
            continue
        if pd.api.types.is_numeric_dtype(s):
            nunique = s.nunique(dropna=True)
            is_id_like = any(w in lname for w in ID_HINT_WORDS) and nunique > max(20, 0.9 * n)
            is_non_additive = any(w in lname for w in NON_ADDITIVE_HINT_WORDS)
            if is_id_like:
                id_like_cols.append(col)
            elif is_non_additive:
                # Dimension-like number (Year, Age, Rank, Jersey Number, ...): keep it
                # available for grouping/filtering but do NOT let it become a Sum/Average
                # KPI or chart measure - that's what was producing nonsense cards.
                if 2 <= nunique <= 50:
                    categorical_cols.append(col)
                else:
                    id_like_cols.append(col)
            else:
                numeric_cols.append(col)
                if 2 <= nunique <= 20:
                    categorical_cols.append(col)  # small-cardinality numeric can double as a dimension
            continue
        # object / string columns
        nunique = s.nunique(dropna=True)
        if any(w in lname for w in ID_HINT_WORDS) and nunique > max(20, 0.8 * n):
            id_like_cols.append(col)
            continue
        if any(w in lname for w in NAME_HINT_WORDS):
            name_like_cols.append(col)
            if nunique > MAX_CARDINALITY_FOR_CATEGORY:
                text_cols.append(col)
                continue
        if any(w in lname for w in STATUS_HINT_WORDS):
            status_cols.append(col)
        if MIN_CARDINALITY_FOR_CATEGORY <= nunique <= MAX_CARDINALITY_FOR_CATEGORY:
            categorical_cols.append(col)
        else:
            text_cols.append(col)

    # pick a primary measure
    primary_measure = None
    if numeric_cols:
        scored = sorted(
            numeric_cols,
            key=lambda c: (any(w in c.lower() for w in MEASURE_HINT_WORDS), df[c].sum() if pd.api.types.is_numeric_dtype(df[c]) else 0),
            reverse=True,
        )
        primary_measure = scored[0]

    primary_date = date_cols[0] if date_cols else None

    return {
        "date_cols": date_cols,
        "numeric_cols": numeric_cols,
        "categorical_cols": list(dict.fromkeys(categorical_cols)),
        "id_like_cols": id_like_cols,
        "text_cols": text_cols,
        "bool_cols": bool_cols,
        "status_cols": status_cols,
        "name_like_cols": name_like_cols,
        "primary_measure": primary_measure,
        "primary_date": primary_date,
        "row_count": n,
    }


# --------------------------------------------------------------------------------
# KPI AUTO-GENERATION
# --------------------------------------------------------------------------------
def _fmt_num(x, number_format: str = "auto"):
    """number_format: "auto" (Cr/L/K, Indian-style abbreviation - default),
    "full" (plain comma-separated, no abbreviation), or "compact" (K/M/B, international)."""
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return "-"
    ax = abs(x)
    if number_format == "full":
        return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"
    if number_format == "compact":
        if ax >= 1e9:
            return f"{x/1e9:,.2f} B"
        if ax >= 1e6:
            return f"{x/1e6:,.2f} M"
        if ax >= 1e3:
            return f"{x/1e3:,.1f} K"
        return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"
    # auto (default, Indian-style)
    if ax >= 1e7:
        return f"{x/1e7:,.2f} Cr"
    if ax >= 1e5:
        return f"{x/1e5:,.2f} L"
    if ax >= 1e3:
        return f"{x/1e3:,.1f} K"
    if float(x).is_integer():
        return f"{int(x):,}"
    return f"{x:,.2f}"


def compute_kpis(df: pd.DataFrame, meta: dict, number_format: str = "auto") -> list:
    """Return a list of dicts: {label, value, sub, delta, column, agg} auto-derived
    from whatever columns exist. `column`/`agg` are set on cards that represent a
    plain column aggregation (Total/Avg/Unique) - the caller can use them to apply
    a per-card filter and recompute just that card, or to bulk-change number_format.
    `number_format` picks the display style for every Total/Avg card at once:
    "auto" (Cr/L/K, default), "full" (plain comma-separated number), "compact" (K/M/B)."""
    kpis = []
    n = len(df)
    kpis.append({"label": "Total Records", "value": f"{n:,}", "sub": "rows in current filter", "delta": None,
                 "column": None, "agg": None})

    for col in meta["numeric_cols"][:6]:
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() == 0:
            continue
        kpis.append({"label": f"Total {col}", "value": _fmt_num(s.sum(), number_format), "sub": "sum", "delta": None,
                     "column": col, "agg": "sum"})
        kpis.append({"label": f"Avg {col}", "value": _fmt_num(s.mean(), number_format), "sub": "average", "delta": None,
                     "column": col, "agg": "mean"})

    for col in meta["id_like_cols"][:4]:
        kpis.append({"label": f"Unique {col}", "value": f"{df[col].nunique():,}", "sub": "distinct", "delta": None,
                     "column": col, "agg": "nunique"})

    for col in meta["name_like_cols"][:2]:
        kpis.append({"label": f"Unique {col}", "value": f"{df[col].nunique():,}", "sub": "distinct people", "delta": None,
                     "column": col, "agg": "nunique"})

    if meta["categorical_cols"]:
        top_col = meta["categorical_cols"][0]
        vc = df[top_col].value_counts()
        if len(vc):
            top_val = vc.index[0]
            share = 100 * vc.iloc[0] / vc.sum()
            kpis.append({"label": f"Top {top_col}", "value": str(top_val), "sub": f"{share:.1f}% of rows", "delta": None})

    if meta["status_cols"]:
        col = meta["status_cols"][0]
        vc = df[col].value_counts(normalize=True) * 100
        if len(vc):
            kpis.append({"label": f"Top {col}", "value": str(vc.index[0]), "sub": f"{vc.iloc[0]:.1f}% share", "delta": None})

    if meta["primary_date"]:
        dcol = meta["primary_date"]
        d = pd.to_datetime(df[dcol], errors="coerce").dropna()
        if len(d):
            kpis.append({"label": "Date Range", "value": f"{d.min().date()} → {d.max().date()}",
                         "sub": f"{(d.max()-d.min()).days} days", "delta": None})
            if meta["primary_measure"]:
                mcol = meta["primary_measure"]
                tmp = df[[dcol, mcol]].dropna()
                tmp[dcol] = pd.to_datetime(tmp[dcol], errors="coerce")
                tmp = tmp.dropna(subset=[dcol])
                if len(tmp) > 4:
                    mid = tmp[dcol].min() + (tmp[dcol].max() - tmp[dcol].min()) / 2
                    first_half = tmp[tmp[dcol] <= mid][mcol].sum()
                    second_half = tmp[tmp[dcol] > mid][mcol].sum()
                    if first_half > 0:
                        growth = 100 * (second_half - first_half) / first_half
                        kpis.append({"label": f"{mcol} Growth (1st half → 2nd half)",
                                     "value": f"{growth:+.1f}%", "sub": "period-over-period", "delta": growth})

    if meta["primary_measure"] and n > 0:
        mcol = meta["primary_measure"]
        s = pd.to_numeric(df[mcol], errors="coerce")
        kpis.append({"label": f"{mcol} per Record", "value": _fmt_num(s.sum() / n), "sub": "average per row", "delta": None})

    return kpis