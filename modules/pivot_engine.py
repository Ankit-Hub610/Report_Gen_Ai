"""
pivot_engine.py
----------------
Phase 6 — Custom Reports / Excel-style Pivot page.

Built on top of the same DuckDB engine as the SQL Query tab (Phase 5,
modules/query_engine.py) so this works smoothly even on very large datasets:
row-level filtering + the GROUP BY aggregation (the expensive part) happens
inside DuckDB, and only the resulting (small) aggregated table is reshaped
into a wide pivot with pandas. A pivot with a few dozen row/column
combinations stays fast even if the underlying table has 100M+ rows.

A "pivot report" is a plain dict so it round-trips through workspace_store's
pickle persistence exactly like custom_kpis / custom_charts already do:

    {
        "id": "8-char-hex",
        "title": "New Pivot Report",
        "rows": ["Region", "Player"],          # ordered group-by fields (row hierarchy)
        "columns": ["Season"],                 # ordered group-by fields (column hierarchy)
        "row_grains": {"Date": "month"},       # optional date bucketing, per field
        "col_grains": {},
        "measures": [{"id":.., "column":.., "agg":"Sum", "label":None,
                       "number_format":"Auto (Cr / L / K)", "custom_format_code":"#,##0.00"}],
        "filters": [...],                      # SAME FilterSpec shape as measures.apply_filters
        "computed_columns": [{"id":.., "name":"Profit", "formula":"Revenue - Cost"}],
        "grand_total": True,
        "header_overrides": {"Sum of Revenue": "Total Revenue"},
        "style": {"header_bg":.., "header_font_color":.., "font_size":.., "border":.., "striped":..},
    }
"""

import re
import uuid

import duckdb
import pandas as pd
import streamlit as st

from . import measures as ms
from . import builder_engine as be

AGG_SQL = {
    "Sum": 'SUM({c})',
    "Average": 'AVG({c})',
    "Count": 'COUNT({c})',
    "Distinct Count": 'COUNT(DISTINCT {c})',
    "Min": 'MIN({c})',
    "Max": 'MAX({c})',
    "Median": 'MEDIAN({c})',
    "Std Dev": 'STDDEV({c})',
}
DATE_GRAIN_OPTS = [("None", None), ("Day", "day"), ("Week", "week"), ("Month", "month"),
                    ("Quarter", "quarter"), ("Year", "year")]

# Same "no funny business" philosophy as query_engine.py — computed-column
# formulas are a single expression dropped into a SELECT list, never a full
# statement, so anything that could stack another statement or touch the
# filesystem is blocked outright.
_BLOCKED = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|"
    r"install|load|pragma|call|vacuum|checkpoint|glob|read_csv(_auto)?|read_parquet|"
    r"read_json(_auto)?|select|union|with)\b",
    re.IGNORECASE,
)


def _q(col: str) -> str:
    """Quote a DuckDB identifier safely (double the internal quotes)."""
    return '"' + str(col).replace('"', '""') + '"'


def _validate_formula(formula: str) -> str:
    """Validates then translates a SQL-style-quoted formula (matching how
    columns are quoted everywhere else in this module/app, e.g. "Revenue" -
    "Cost") into pandas .eval() syntax, which needs backtick quoting for
    identifiers instead (`Revenue` - `Cost`)."""
    f = (formula or "").strip()
    if not f:
        raise ValueError("Formula is empty.")
    if ";" in f or "--" in f or "/*" in f:
        raise ValueError("Only a single expression is allowed (no ';' or comments).")
    if _BLOCKED.search(re.sub(r'"[^"]*"', "", f)):
        raise ValueError("That formula uses a keyword that isn't allowed here — plain expressions only "
                          "(e.g. \"Revenue\" - \"Cost\", or \"Revenue\" * 0.1).")
    return re.sub(r'"([^"]+)"', r'`\1`', f)


# ==================================================================================
# REPORT LIFECYCLE
# ==================================================================================
def new_pivot_report(df) -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "title": "New Pivot Report",
        "rows": [],
        "columns": [],
        "row_grains": {},
        "col_grains": {},
        "measures": [],
        "filters": [],
        "computed_columns": [],
        "grand_total": True,
        "header_overrides": {},
        # Independent KPI cards — each one its own Column + Aggregation, completely
        # separate from the table's Rows/Columns/Measures above (e.g. a "Distinct
        # Count of department" card even though department is only a Row in the
        # table, never one of its Measures). See compute_global_measures().
        "global_measures": [],
        "style": {"header_bg": "#2C6E49", "header_font_color": "#FFFFFF", "font_size": 13,
                   "border": True, "striped": True},
    }


def _computed_df(df: pd.DataFrame, report: dict) -> pd.DataFrame:
    """Applies computed columns (pandas .eval, on the already-filtered frame)
    and returns a NEW dataframe — never mutates the caller's df."""
    out = df
    ccs = report.get("computed_columns", [])
    if not ccs:
        return out
    out = out.copy()
    for cc in ccs:
        try:
            formula = _validate_formula(cc["formula"])
            out[cc["name"]] = out.eval(formula, engine="python")
        except Exception as e:
            cc["_error"] = str(e)
        else:
            cc["_error"] = None
    return out


# ==================================================================================
# BUILD (DuckDB group-by -> pandas reshape)
# ==================================================================================
def build_pivot_table(df: pd.DataFrame, report: dict):
    """Returns (display_df, raw_numeric_df, error) — error is a human-readable
    string (or None). raw_numeric_df keeps real numbers (for CSV/Excel export),
    display_df has each measure Excel-formatted for on-screen viewing."""
    rows = report.get("rows", [])
    cols = report.get("columns", [])
    measure_defs = report.get("measures", [])
    if not rows and not cols:
        return None, None, "Pick at least one field for Rows or Columns."
    if not measure_defs:
        return None, None, "Add at least one measure."

    df_f = ms.apply_filters(df, report.get("filters", []))
    df_f = _computed_df(df_f, report)
    if len(df_f) == 0:
        return None, None, "No rows left after filters."

    row_grains = report.get("row_grains", {})
    col_grains = report.get("col_grains", {})

    def group_expr(col, grain):
        if grain:
            return f"date_trunc('{grain}', TRY_CAST({_q(col)} AS TIMESTAMP))"
        return _q(col)

    select_parts, group_positions, row_aliases, col_aliases, meas_aliases = [], [], [], [], []
    pos = 1
    for c in rows:
        select_parts.append(f"{group_expr(c, row_grains.get(c))} AS {_q(c)}")
        group_positions.append(str(pos)); pos += 1; row_aliases.append(c)
    for c in cols:
        select_parts.append(f"{group_expr(c, col_grains.get(c))} AS {_q(c)}")
        group_positions.append(str(pos)); pos += 1; col_aliases.append(c)

    used_labels = set()
    for m in measure_defs:
        agg_tpl = AGG_SQL.get(m["agg"], AGG_SQL["Sum"])
        col_sql = "*" if m["agg"] == "Count" and m["column"] in (None, "(all rows)") else _q(m["column"])
        agg_sql = agg_tpl.format(c=col_sql)
        label = m.get("label") or f"{m['agg']} of {m['column']}"
        while label in used_labels:
            label += " "
        used_labels.add(label)
        m["_resolved_label"] = label
        select_parts.append(f"{agg_sql} AS {_q(label)}")
        meas_aliases.append(label)

    if not group_positions:
        return None, None, "Pick at least one field for Rows or Columns."

    sql = f"SELECT {', '.join(select_parts)} FROM t GROUP BY {', '.join(group_positions)}"

    con = duckdb.connect(database=":memory:")
    try:
        con.execute("SET enable_external_access=false")
        con.register("t", df_f)
        agg_df = con.execute(sql).df()
    except duckdb.Error as e:
        return None, None, f"Could not build the pivot: {e}"
    finally:
        con.close()

    try:
        if col_aliases:
            wide = agg_df.pivot_table(index=row_aliases or None, columns=col_aliases,
                                       values=meas_aliases, aggfunc="first")
            if len(meas_aliases) == 1:
                wide.columns = [" | ".join([str(x) for x in c]) if isinstance(c, tuple) else str(c)
                                 for c in wide.columns]
            else:
                wide.columns = [" | ".join([str(x) for x in c]) for c in wide.columns]
            raw_df = wide.reset_index() if row_aliases else wide.reset_index(drop=True)
        else:
            raw_df = agg_df.set_index(row_aliases) if row_aliases else agg_df
            raw_df = raw_df.reset_index() if row_aliases else raw_df
    except Exception as e:
        return None, None, f"Could not reshape the pivot: {e}"

    if report.get("grand_total", True) and row_aliases:
        num_cols = [c for c in raw_df.columns if c not in row_aliases and pd.api.types.is_numeric_dtype(raw_df[c])]
        if num_cols:
            total_row = {c: "Grand Total" if c == row_aliases[0] else "" for c in row_aliases}
            for c in num_cols:
                total_row[c] = raw_df[c].sum()
            raw_df = pd.concat([raw_df, pd.DataFrame([total_row])], ignore_index=True)

    overrides = report.get("header_overrides", {})
    raw_df = raw_df.rename(columns={k: v for k, v in overrides.items() if k in raw_df.columns})

    # Build the display copy with Excel-style number formatting per measure.
    display_df = raw_df.copy()
    fmt_by_label = {}
    for m in measure_defs:
        lbl = overrides.get(m.get("_resolved_label"), m.get("_resolved_label"))
        fmt_by_label[lbl] = m

    for c in display_df.columns:
        m = None
        for lbl, mm in fmt_by_label.items():
            if str(c) == lbl or str(c).startswith(lbl + " | "):
                m = mm
                break
        if m is not None and pd.api.types.is_numeric_dtype(display_df[c]):
            fmt_code = ms.NUMBER_FORMAT_PRESETS.get(m.get("number_format", "Auto (Cr / L / K)"), "auto")
            display_df[c] = display_df[c].apply(
                lambda v, fc=fmt_code, cc=m.get("custom_format_code", ""): "" if pd.isna(v) else ms.format_value(v, fc, cc)
            )

    return display_df, raw_df, None


def measure_grand_totals(raw_df: pd.DataFrame, report: dict) -> list:
    """One KPI-card's worth of data per measure — the grand total, Excel-
    formatted the same way the table itself formats that measure. Backs the
    card strip shown above the pivot table, so the headline numbers are
    visible at a glance before scrolling into the row/column detail."""
    if raw_df is None or raw_df.empty:
        return []
    row_aliases = report.get("rows", [])
    overrides = report.get("header_overrides", {})
    body = raw_df
    if row_aliases and row_aliases[0] in raw_df.columns:
        body = raw_df[raw_df[row_aliases[0]] != "Grand Total"]
    cards = []
    for m in report.get("measures", []):
        lbl = overrides.get(m.get("_resolved_label"), m.get("_resolved_label"))
        if not lbl:
            continue
        matching_cols = [c for c in body.columns
                          if (str(c) == lbl or str(c).startswith(str(lbl) + " | "))
                          and pd.api.types.is_numeric_dtype(body[c])]
        if not matching_cols:
            continue
        total = body[matching_cols].sum().sum()
        fmt_code = ms.NUMBER_FORMAT_PRESETS.get(m.get("number_format", "Auto (Cr / L / K)"), "auto")
        cards.append({"label": lbl, "value": ms.format_value(total, fmt_code, m.get("custom_format_code", "")),
                      "raw_value": total})
    return cards


# ==================================================================================
# GLOBAL MEASURES / CARDS — independent of the table's Rows/Columns/Measures.
# Each card is its own Column + Aggregation, computed straight off the
# (filtered) base data — it never has to match anything used in the pivot
# table itself. Two filter layers apply, same "sabka ek filter + apna alag
# filter" idea as the table: `report["filters"]` (shared — also narrows the
# table/chart) is applied FIRST, then this card's own `gm["filters"]` (only
# this card) on top of that.
# ==================================================================================
def compute_global_measures(df: pd.DataFrame, report: dict) -> list:
    """Returns one dict per configured card:
    {"id", "label", "value", "raw_value", "error"} — error is None on success,
    a short human-readable string if that one card's own filter/column broke
    (a bad card never takes the others down with it)."""
    shared_filtered = ms.apply_filters(df, report.get("filters", []))
    out = []
    for gm in report.get("global_measures", []):
        label = gm.get("label") or (
            f"{gm['agg']} of rows" if gm.get("column") in (None, "(all rows)") else f"{gm['agg']} of {gm['column']}"
        )
        card = {"id": gm["id"], "label": label, "value": "—", "raw_value": None, "error": None}
        try:
            card_df = ms.apply_filters(shared_filtered, gm.get("filters", []))
            if len(card_df) == 0:
                card["error"] = "No rows left after this card's filter."
                out.append(card)
                continue
            agg_tpl = AGG_SQL.get(gm["agg"], AGG_SQL["Sum"])
            is_all_rows = gm.get("column") in (None, "(all rows)")
            col_sql = "*" if (gm["agg"] == "Count" and is_all_rows) else _q(gm["column"])
            if is_all_rows and gm["agg"] != "Count":
                card["error"] = "Pick a column for this aggregation (only Count works on '(all rows)')."
                out.append(card)
                continue
            agg_sql = agg_tpl.format(c=col_sql)
            con = duckdb.connect(database=":memory:")
            try:
                con.execute("SET enable_external_access=false")
                con.register("t", card_df)
                raw_value = con.execute(f"SELECT {agg_sql} AS v FROM t").df()["v"].iloc[0]
            finally:
                con.close()
            fmt_code = ms.NUMBER_FORMAT_PRESETS.get(gm.get("number_format", "Auto (Cr / L / K)"), "auto")
            card["raw_value"] = raw_value
            card["value"] = ms.format_value(raw_value, fmt_code, gm.get("custom_format_code", ""))
        except Exception as e:
            card["error"] = str(e)
        out.append(card)
    return out


def render_global_measures_builder(df: pd.DataFrame, report: dict, key_prefix: str):
    """The '+ Add global measure' UI — Excel-style: pick a Column + an
    Aggregation, optionally rename it and give IT its own filter, and it
    becomes a standalone KPI card. Completely independent of the table
    above — add as many as you like."""
    all_cols = ms.list_all_columns(df)
    col_names = [c["name"] for c in all_cols]
    computed_names = [cc["name"] for cc in report.get("computed_columns", [])]
    pickable = ["(all rows)"] + col_names + computed_names

    remove_gm = None
    for gm in report.get("global_measures", []):
        with st.container(border=True):
            gc1, gc2, gc3, gc4 = st.columns([2, 2, 2, 1])
            with gc1:
                gm["column"] = st.selectbox("Column", pickable,
                                            index=pickable.index(gm["column"]) if gm.get("column") in pickable else 0,
                                            key=f"{key_prefix}gmcol_{gm['id']}")
            with gc2:
                gm["agg"] = st.selectbox("Aggregation", ms.ALL_MEASURES[:8],
                                         index=ms.ALL_MEASURES[:8].index(gm["agg"]) if gm.get("agg") in ms.ALL_MEASURES[:8] else 0,
                                         key=f"{key_prefix}gmagg_{gm['id']}")
            with gc3:
                gm["label"] = st.text_input("Card title (optional)", gm.get("label") or "",
                                            key=f"{key_prefix}gmlab_{gm['id']}")
            with gc4:
                st.write("")
                if st.button("🗑️", key=f"{key_prefix}gmdel_{gm['id']}"):
                    remove_gm = gm["id"]
            fmt_opts = list(ms.NUMBER_FORMAT_PRESETS.keys())
            cur_fmt = gm.get("number_format", "Auto (Cr / L / K)")
            gm["number_format"] = st.selectbox("Format", fmt_opts,
                                               index=fmt_opts.index(cur_fmt) if cur_fmt in fmt_opts else 0,
                                               key=f"{key_prefix}gmfmt_{gm['id']}")
            if ms.NUMBER_FORMAT_PRESETS.get(gm["number_format"]) == "custom":
                gm["custom_format_code"] = st.text_input("Excel format code", gm.get("custom_format_code", "#,##0.00"),
                                                          key=f"{key_prefix}gmfmtcode_{gm['id']}")
            with st.expander(f"🔍 This card's own filter (on top of the shared filter below)"):
                gm["filters"] = be.render_filter_builder(df, gm.get("filters", []),
                                                          key_prefix=f"{key_prefix}gmf_{gm['id']}_")
    if remove_gm is not None:
        report["global_measures"] = [g for g in report["global_measures"] if g["id"] != remove_gm]
        st.rerun()
    if st.button("➕ Add global measure", key=f"{key_prefix}gmadd"):
        report.setdefault("global_measures", []).append(
            {"id": uuid.uuid4().hex[:8], "column": pickable[0] if pickable else "(all rows)", "agg": "Count",
             "label": "", "number_format": "Auto (Cr / L / K)", "custom_format_code": "#,##0.00", "filters": []})
        st.rerun()


# ==================================================================================
# BUILDER UI
# ==================================================================================
def render_pivot_builder(df: pd.DataFrame, report: dict, key_prefix: str):
    all_cols = ms.list_all_columns(df)
    col_names = [c["name"] for c in all_cols]
    kind_by_name = {c["name"]: c["kind"] for c in all_cols}
    computed_names = [cc["name"] for cc in report.get("computed_columns", [])]
    pickable = col_names + computed_names

    report["title"] = st.text_input("Report title", report["title"], key=f"{key_prefix}title_{report['id']}")

    with st.expander("➕ Computed columns (e.g. Revenue - Cost)", expanded=False):
        remove_idx = None
        for idx, cc in enumerate(report.get("computed_columns", [])):
            cc1, cc2, cc3 = st.columns([2, 3, 1])
            with cc1:
                cc["name"] = st.text_input("Name", cc["name"], key=f"{key_prefix}ccname_{cc['id']}")
            with cc2:
                cc["formula"] = st.text_input("Formula", cc["formula"], key=f"{key_prefix}ccform_{cc['id']}",
                                               help='Column names in quotes, e.g. "Revenue" - "Cost", or "Revenue" * 0.1')
            with cc3:
                st.write("")
                if st.button("🗑️", key=f"{key_prefix}ccdel_{cc['id']}"):
                    remove_idx = idx
            if cc.get("_error"):
                st.error(cc["_error"])
        if remove_idx is not None:
            report["computed_columns"].pop(remove_idx)
            st.rerun()
        if st.button("➕ Add computed column", key=f"{key_prefix}ccadd"):
            report.setdefault("computed_columns", []).append(
                {"id": uuid.uuid4().hex[:8], "name": f"Computed {len(report.get('computed_columns', [])) + 1}", "formula": ""})
            st.rerun()

    st.markdown("**Rows / Columns (drag-select — pick fields, order = hierarchy)**")
    r1, r2 = st.columns(2)
    with r1:
        report["rows"] = st.multiselect("Rows", pickable, default=[c for c in report.get("rows", []) if c in pickable],
                                         key=f"{key_prefix}rows_{report['id']}")
        for c in report["rows"]:
            if kind_by_name.get(c) == "date":
                labels = [g[0] for g in DATE_GRAIN_OPTS]; codes = [g[1] for g in DATE_GRAIN_OPTS]
                cur = codes.index(report["row_grains"].get(c)) if report["row_grains"].get(c) in codes else 0
                report["row_grains"][c] = codes[labels.index(
                    st.selectbox(f"↳ {c} granularity", labels, index=cur, key=f"{key_prefix}rg_{report['id']}_{c}"))]
    with r2:
        col_opts = [c for c in pickable if c not in report["rows"]]
        report["columns"] = st.multiselect("Columns", col_opts, default=[c for c in report.get("columns", []) if c in col_opts],
                                            key=f"{key_prefix}cols_{report['id']}")
        for c in report["columns"]:
            if kind_by_name.get(c) == "date":
                labels = [g[0] for g in DATE_GRAIN_OPTS]; codes = [g[1] for g in DATE_GRAIN_OPTS]
                cur = codes.index(report["col_grains"].get(c)) if report["col_grains"].get(c) in codes else 0
                report["col_grains"][c] = codes[labels.index(
                    st.selectbox(f"↳ {c} granularity", labels, index=cur, key=f"{key_prefix}cg_{report['id']}_{c}"))]

    st.markdown("**Measures**")
    remove_m = None
    for idx, m in enumerate(report.get("measures", [])):
        mc1, mc2, mc3, mc4, mc5 = st.columns([2, 2, 2, 2, 1])
        with mc1:
            m["column"] = st.selectbox("Column", pickable, index=pickable.index(m["column"]) if m["column"] in pickable else 0,
                                        key=f"{key_prefix}mcol_{m['id']}")
        with mc2:
            m["agg"] = st.selectbox("Aggregation", ms.ALL_MEASURES[:8], index=ms.ALL_MEASURES[:8].index(m["agg"]) if m["agg"] in ms.ALL_MEASURES[:8] else 0,
                                     key=f"{key_prefix}magg_{m['id']}")
        with mc3:
            m["label"] = st.text_input("Label (optional)", m.get("label") or "", key=f"{key_prefix}mlab_{m['id']}")
        with mc4:
            fmt_opts = list(ms.NUMBER_FORMAT_PRESETS.keys())
            cur_fmt = m.get("number_format", "Auto (Cr / L / K)")
            m["number_format"] = st.selectbox("Format", fmt_opts, index=fmt_opts.index(cur_fmt) if cur_fmt in fmt_opts else 0,
                                               key=f"{key_prefix}mfmt_{m['id']}")
        with mc5:
            st.write("")
            if st.button("🗑️", key=f"{key_prefix}mdel_{m['id']}"):
                remove_m = idx
        if ms.NUMBER_FORMAT_PRESETS.get(m["number_format"]) == "custom":
            m["custom_format_code"] = st.text_input("Excel format code", m.get("custom_format_code", "#,##0.00"),
                                                      key=f"{key_prefix}mfmtcode_{m['id']}")
    if remove_m is not None:
        report["measures"].pop(remove_m)
        st.rerun()
    if st.button("➕ Add measure", key=f"{key_prefix}madd"):
        report.setdefault("measures", []).append(
            {"id": uuid.uuid4().hex[:8], "column": pickable[0] if pickable else None, "agg": "Sum",
             "label": "", "number_format": "Auto (Cr / L / K)", "custom_format_code": "#,##0.00"})
        st.rerun()

    report["filters"] = be.render_filter_builder(df, report.get("filters", []), key_prefix=f"{key_prefix}pf_{report['id']}_")

    report["grand_total"] = st.checkbox("Show grand total row", report.get("grand_total", True), key=f"{key_prefix}gt_{report['id']}")


def render_pivot_style_editor(report: dict, key_prefix: str):
    style = report.setdefault("style", {"header_bg": "#2C6E49", "header_font_color": "#FFFFFF", "font_size": 13,
                                          "border": True, "striped": True})
    s1, s2, s3, s4, s5 = st.columns(5)
    with s1:
        style["header_bg"] = st.color_picker("Header background", style["header_bg"], key=f"{key_prefix}sbg_{report['id']}")
    with s2:
        style["header_font_color"] = st.color_picker("Header font color", style["header_font_color"], key=f"{key_prefix}sfc_{report['id']}")
    with s3:
        style["font_size"] = st.slider("Font size", 10, 20, style["font_size"], key=f"{key_prefix}sfs_{report['id']}")
    with s4:
        style["border"] = st.checkbox("Cell borders", style["border"], key=f"{key_prefix}sbd_{report['id']}")
    with s5:
        style["striped"] = st.checkbox("Striped rows", style["striped"], key=f"{key_prefix}sst_{report['id']}")


def render_header_rename_ui(report: dict, display_df: pd.DataFrame, key_prefix: str):
    with st.expander("✏️ Rename column headers (Excel-style)", expanded=False):
        overrides = report.setdefault("header_overrides", {})
        for c in display_df.columns:
            base = next((k for k, v in overrides.items() if v == c), c)
            new_lbl = st.text_input(f"'{base}' shown as", overrides.get(base, base), key=f"{key_prefix}hdr_{report['id']}_{base}")
            if new_lbl and new_lbl != base:
                overrides[base] = new_lbl
            elif base in overrides and new_lbl == base:
                overrides.pop(base, None)


# ==================================================================================
# RENDER (Excel-style HTML table honoring style dict)
# ==================================================================================
def render_pivot_table(display_df: pd.DataFrame, report: dict):
    if display_df is None or display_df.empty:
        st.info("Nothing to show yet.")
        return
    style = report.get("style", {})
    border = "1px solid #444" if style.get("border", True) else "none"
    header_bg = style.get("header_bg", "#2C6E49")
    header_fc = style.get("header_font_color", "#FFFFFF")
    font_size = style.get("font_size", 13)
    striped = style.get("striped", True)

    css = f"""
    <style>
    .pivot-wrap {{ overflow-x:auto; }}
    .pivot-table {{ border-collapse: collapse; font-size:{font_size}px; width:100%; }}
    .pivot-table th {{ background:{header_bg}; color:{header_fc}; border:{border}; padding:6px 10px; text-align:left; position:sticky; top:0; }}
    .pivot-table td {{ border:{border}; padding:5px 10px; }}
    .pivot-table tr:last-child td {{ font-weight:700; border-top:2px solid {header_bg}; }}
    {f'.pivot-table tbody tr:nth-child(even) td {{ background: rgba(255,255,255,0.04); }}' if striped else ''}
    </style>
    """
    html = display_df.to_html(classes="pivot-table", index=False, border=0, na_rep="")
    st.markdown(css + f"<div class='pivot-wrap'>{html}</div>", unsafe_allow_html=True)


# ==================================================================================
# PIVOTCHART — a chart built directly from the pivot's own (already-aggregated,
# already-grouped) result, same idea as Excel's PivotChart. Deliberately simple:
# operates on raw_df (real numbers, not the Excel-formatted display strings) and
# needs at least one Row field to have an x-axis to plot against.
# ==================================================================================
CHART_TYPES = ["Bar", "Line", "Pie"]


def build_pivot_chart(raw_df: pd.DataFrame, report: dict, chart_type: str = "Bar"):
    """Returns a plotly Figure, or None if there's nothing sensible to chart
    (no Row field, or no numeric measure columns)."""
    import plotly.graph_objects as go

    rows = report.get("rows", [])
    if not rows or raw_df is None or raw_df.empty:
        return None

    plot_df = raw_df[raw_df[rows[0]] != "Grand Total"].copy() if rows else raw_df.copy()
    if plot_df.empty:
        return None

    if len(rows) == 1:
        x_labels = plot_df[rows[0]].astype(str)
    else:
        x_labels = plot_df[rows].astype(str).agg(" / ".join, axis=1)

    measure_cols = [c for c in plot_df.columns if c not in rows and pd.api.types.is_numeric_dtype(plot_df[c])]
    if not measure_cols:
        return None
    # Keep charts readable — cap at 8 measure/series columns and 30 x-axis categories
    measure_cols = measure_cols[:8]
    if len(x_labels) > 30:
        plot_df = plot_df.iloc[:30]
        x_labels = x_labels.iloc[:30]

    fig = go.Figure()
    style = report.get("style", {})
    palette = ["#2C6E49", "#3969AC", "#E68310", "#7F3C8D", "#11A579", "#F2B701", "#E73F74", "#80BA5A"]

    if chart_type == "Pie":
        # Pie only really makes sense for a single measure — use the first one
        m = measure_cols[0]
        fig.add_trace(go.Pie(labels=x_labels, values=plot_df[m], hole=0.35))
        fig.update_layout(title=f"{report.get('title', 'Pivot')} — {m}")
    else:
        trace_cls = go.Bar if chart_type == "Bar" else go.Scatter
        for i, m in enumerate(measure_cols):
            kwargs = {"x": x_labels, "y": plot_df[m], "name": m}
            if chart_type == "Bar":
                kwargs["marker_color"] = palette[i % len(palette)]
            else:
                kwargs["mode"] = "lines+markers"
                kwargs["line"] = dict(color=palette[i % len(palette)])
            fig.add_trace(trace_cls(**kwargs))
        fig.update_layout(barmode="group", title=report.get("title", "Pivot Chart"),
                          xaxis_title=" / ".join(rows), legend_title_text="")

    fig.update_layout(template="plotly_white", font=dict(size=style.get("font_size", 13)),
                      margin=dict(l=40, r=20, t=50, b=40), height=420)
    return fig


# ==================================================================================
# PDF EXPORT — table + optional chart, one pivot report per call. Kept
# self-contained (own reportlab flow) rather than reusing pdf_export.py's
# Boss-Dashboard-shaped report builder, since a pivot report's layout
# (one table, optionally wide) is different enough to want its own simple flow.
# ==================================================================================
def export_pivot_pdf(report: dict, display_df: pd.DataFrame, chart_png_bytes: bytes = None) -> bytes:
    import io
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import cm
    from reportlab.lib import colors as rl_colors
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.enums import TA_CENTER

    buf = io.BytesIO()
    is_wide = len(display_df.columns) > 6
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4) if is_wide else A4,
                            topMargin=1.5 * cm, bottomMargin=1.5 * cm, leftMargin=1.2 * cm, rightMargin=1.2 * cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PivotTitle", parent=styles["Title"], alignment=TA_CENTER,
                                 textColor=rl_colors.HexColor(report.get("style", {}).get("header_bg", "#2C6E49")))
    story = [Paragraph(report.get("title", "Pivot Report"), title_style), Spacer(1, 10)]

    if chart_png_bytes:
        story.append(RLImage(io.BytesIO(chart_png_bytes), width=24 * cm if is_wide else 16 * cm,
                             height=(24 * cm if is_wide else 16 * cm) * 0.42))
        story.append(Spacer(1, 14))

    header_bg = rl_colors.HexColor(report.get("style", {}).get("header_bg", "#2C6E49"))
    header_fc = rl_colors.HexColor(report.get("style", {}).get("header_font_color", "#FFFFFF"))
    striped = report.get("style", {}).get("striped", True)

    table_data = [list(display_df.columns)] + display_df.astype(str).values.tolist()
    tbl = Table(table_data, repeatRows=1)
    tstyle = [
        ("BACKGROUND", (0, 0), (-1, 0), header_bg),
        ("TEXTCOLOR", (0, 0), (-1, 0), header_fc),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.HexColor("#999999")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),   # grand-total row, if present
    ]
    if striped:
        for r in range(1, len(table_data)):
            if r % 2 == 0:
                tstyle.append(("BACKGROUND", (0, r), (-1, r), rl_colors.HexColor("#F2F2F2")))
    tbl.setStyle(TableStyle(tstyle))
    story.append(tbl)

    doc.build(story)
    return buf.getvalue()
