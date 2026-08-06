"""
builder_engine.py
------------------
The "Custom Builder" (Power-BI style) layer on top of the auto-generated
Raw Analysis. Two things live here:

  1. A reusable per-item FILTER BUILDER widget - every KPI card and every
     chart gets its OWN independent set of filters (Basic or Advanced),
     built from ALL columns in the dataset (nothing excluded).

  2. A generic CHART BUILDER - user freely assigns:
       X field   = any column (+ date granularity if it's a date)
       Y field   = any column + a measure/aggregation (Sum, Average, Count,
                   Distinct Count, Min, Max, Median, Std Dev)
       Color/Legend field (optional) = any categorical column
     ...and a chart type, exactly like Power BI's field wells.

Nothing about the existing Raw Analysis / Boss Dashboard / Data Table pages
is removed - this module only adds the new capability.
"""

import uuid

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from . import measures as ms

CHART_TYPES = ["Bar", "Line", "Pie", "Donut", "Area", "Scatter", "Box", "Histogram", "Treemap", "Heatmap", "Table"]
CHART_ICONS = {
    "Bar": "📊", "Line": "📈", "Pie": "🥧", "Donut": "🍩", "Area": "🏔️",
    "Scatter": "🔵", "Box": "📦", "Histogram": "📶", "Treemap": "🌳", "Heatmap": "🔥", "Table": "🗂",
}
DATE_GRAINS = [("None", None), ("Daily", "D"), ("Weekly", "W"), ("Monthly", "ME"), ("Yearly", "YE")]


# ==================================================================================
# PER-ITEM FILTER BUILDER (used by both KPI cards and charts)
# ==================================================================================
def render_filter_builder(df: pd.DataFrame, filters: list, key_prefix: str) -> list:
    """Renders an 'Add filter' UI for ONE card/chart and returns the updated filter list.
    Each filter row lets the user pick Basic (quick dropdown/slider) or Advanced
    (operator based: contains, >, between, is blank, ...) mode, on ANY column."""
    all_cols = ms.list_all_columns(df)
    col_names = [c["name"] for c in all_cols]
    kind_by_name = {c["name"]: c["kind"] for c in all_cols}

    st.caption("🔎 Filters for this item")
    if filters:
        chips = " · ".join(ms.describe_filter(f) for f in filters)
        st.caption(f"Active: {chips}")

    with st.popover("➕ Add / edit filters", use_container_width=True):
        if not filters:
            st.caption("No filters yet on this item.")
        remove_idx = None
        for idx, f in enumerate(filters):
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                with c1:
                    new_col = st.selectbox("Column", col_names,
                                            index=col_names.index(f["column"]) if f["column"] in col_names else 0,
                                            key=f"{key_prefix}fc_{f['id']}")
                with c2:
                    new_mode = st.radio("Mode", ["Basic", "Advanced"],
                                         index=0 if f.get("mode", "basic") == "basic" else 1,
                                         key=f"{key_prefix}fm_{f['id']}", horizontal=True)
                with c3:
                    st.write("")
                    if st.button("🗑️", key=f"{key_prefix}fdel_{f['id']}"):
                        remove_idx = idx
                f["column"] = new_col
                f["kind"] = kind_by_name.get(new_col, "text")
                f["mode"] = "basic" if new_mode == "Basic" else "advanced"
                _render_filter_value_inputs(df, f, key_prefix)
            st.write("")
        if remove_idx is not None:
            filters.pop(remove_idx)
            st.rerun()

        if st.button("➕ Add another filter", key=f"{key_prefix}addfilter"):
            filters.append({"id": uuid.uuid4().hex[:8], "column": col_names[0] if col_names else None,
                             "kind": kind_by_name.get(col_names[0]) if col_names else "text",
                             "mode": "basic"})
            st.rerun()

    return filters


def _render_filter_value_inputs(df, f, key_prefix):
    col, kind, mode = f["column"], f["kind"], f["mode"]
    if col not in df.columns:
        return
    s = df[col]

    if mode == "basic":
        if kind in ("categorical", "text", "boolean"):
            opts = sorted([str(x) for x in s.dropna().unique()])[:500]
            f["values"] = st.multiselect("Values", opts, default=f.get("values", []),
                                          key=f"{key_prefix}fv_{f['id']}")
        elif kind == "numeric":
            num = pd.to_numeric(s, errors="coerce").dropna()
            if len(num):
                lo, hi = float(num.min()), float(num.max())
                if lo < hi:
                    f["range"] = st.slider("Range", lo, hi, f.get("range", (lo, hi)), key=f"{key_prefix}fr_{f['id']}")
        elif kind == "date":
            d = pd.to_datetime(s, errors="coerce").dropna()
            if len(d):
                lo, hi = d.min().date(), d.max().date()
                existing = f.get("range", (lo, hi))
                if not (isinstance(existing, (tuple, list)) and len(existing) == 2):
                    existing = (lo, hi)
                picked = st.date_input("Range", existing, key=f"{key_prefix}fd_{f['id']}")
                # st.date_input returns a single date (not a 2-tuple) while the
                # user has only picked the start of the range - normalise so
                # f["range"] is ALWAYS a proper (start, end) pair, never a bare
                # date. This is what was causing the "not enough values to
                # unpack (expected 2, got 1)" crash.
                if isinstance(picked, (tuple, list)) and len(picked) == 2:
                    f["range"] = (picked[0], picked[1])
                elif isinstance(picked, (tuple, list)) and len(picked) == 1:
                    f["range"] = (picked[0], picked[0])
                elif picked:
                    f["range"] = (picked, picked)
    else:
        ops = ms.OPERATORS_BY_KIND.get(kind, ["is any of"])
        f["operator"] = st.selectbox("Operator", ops,
                                      index=ops.index(f["operator"]) if f.get("operator") in ops else 0,
                                      key=f"{key_prefix}fop_{f['id']}")
        op = f["operator"]
        if op in ("is blank", "is not blank", "is true", "is false"):
            return
        if op in ("is any of", "is none of"):
            opts = sorted([str(x) for x in s.dropna().unique()])[:500]
            f["value"] = st.multiselect("Value(s)", opts, default=f.get("value", []) if isinstance(f.get("value"), list) else [],
                                         key=f"{key_prefix}fval_{f['id']}")
        elif op == "between":
            c1, c2 = st.columns(2)
            if kind == "date":
                with c1:
                    f["value"] = st.date_input("From", key=f"{key_prefix}fval1_{f['id']}")
                with c2:
                    f["value2"] = st.date_input("To", key=f"{key_prefix}fval2_{f['id']}")
            else:
                with c1:
                    f["value"] = st.number_input("From", key=f"{key_prefix}fval1_{f['id']}", value=float(f.get("value") or 0))
                with c2:
                    f["value2"] = st.number_input("To", key=f"{key_prefix}fval2_{f['id']}", value=float(f.get("value2") or 0))
        elif kind == "date" and op in ("on", "before", "after"):
            f["value"] = st.date_input("Date", key=f"{key_prefix}fval_{f['id']}")
        elif kind == "date" and op == "in last N days":
            f["value"] = st.number_input("N days", min_value=1, value=int(f.get("value") or 30), key=f"{key_prefix}fval_{f['id']}")
        elif kind == "numeric":
            f["value"] = st.number_input("Value", key=f"{key_prefix}fval_{f['id']}", value=float(f.get("value") or 0))
        else:
            f["value"] = st.text_input("Value", value=f.get("value") or "", key=f"{key_prefix}fval_{f['id']}")


# ==================================================================================
# CUSTOM KPI CARD
# ==================================================================================
def new_kpi_card(df, default_measure=None, default_format=None):
    all_cols = ms.list_all_columns(df)
    first = all_cols[0]["name"] if all_cols else None
    return {
        "id": uuid.uuid4().hex[:8],
        "title": "New KPI Card",
        "column": first,
        "measure": default_measure or ms.measures_for_kind(all_cols[0]["kind"] if all_cols else "text")[0],
        "filters": [],
        "pinned": False,
        "number_format": default_format or "Auto (Cr / L / K)",
        "custom_format_code": "#,##0.00",
    }


def render_kpi_card_editor(df, card, key_prefix):
    all_cols = ms.list_all_columns(df)
    col_names = [c["name"] for c in all_cols]
    kind_by_name = {c["name"]: c["kind"] for c in all_cols}

    card["title"] = st.text_input("Card title", card["title"], key=f"{key_prefix}title_{card['id']}")
    c1, c2 = st.columns(2)
    with c1:
        card["column"] = st.selectbox("Column", col_names,
                                       index=col_names.index(card["column"]) if card["column"] in col_names else 0,
                                       key=f"{key_prefix}col_{card['id']}")
    with c2:
        opts = ms.measures_for_kind(kind_by_name.get(card["column"], "text"))
        card["measure"] = st.selectbox("Measure", opts,
                                        index=opts.index(card["measure"]) if card["measure"] in opts else 0,
                                        key=f"{key_prefix}meas_{card['id']}")

    fmt_opts = list(ms.NUMBER_FORMAT_PRESETS.keys())
    cur_fmt = card.get("number_format", "Auto (Cr / L / K)")
    card["number_format"] = st.selectbox(
        "Number format (Excel-style)", fmt_opts,
        index=fmt_opts.index(cur_fmt) if cur_fmt in fmt_opts else 0,
        key=f"{key_prefix}fmt_{card['id']}",
        help="Same idea as Excel's 'Format Cells' — pick a preset, or 'Custom' to type your own format code.",
    )
    if ms.NUMBER_FORMAT_PRESETS.get(card["number_format"]) == "custom":
        card["custom_format_code"] = st.text_input(
            "Excel format code", value=card.get("custom_format_code", "#,##0.00"),
            key=f"{key_prefix}fmtcode_{card['id']}",
            help='Examples: #,##0.00  |  $#,##0  |  0.00%  |  #,##0,"K"',
        )

    card["filters"] = render_filter_builder(df, card.get("filters", []), key_prefix=f"{key_prefix}kpi_{card['id']}_")


def render_kpi_card_value(df, card):
    fdf = ms.apply_filters(df, card.get("filters", []))
    value, sub = ms.compute_measure(fdf, card["column"], card["measure"])
    fmt_code = ms.NUMBER_FORMAT_PRESETS.get(card.get("number_format", "Auto (Cr / L / K)"), "auto")
    display_value = ms.format_value(value, fmt_code, card.get("custom_format_code", ""))
    st.metric(card["title"], display_value,
              help=f"{card['measure']} of {card['column']} · {sub} · {len(fdf):,} rows after this card's filters")


def render_global_kpi_toolbar(df, cards, key_prefix="global_"):
    """A single 'apply to every card' control: pick one measure + one number
    format and push it onto ALL existing KPI cards at once. Each card still
    keeps its OWN independent filters — this only overrides measure/format."""
    if not cards:
        return
    all_cols = ms.list_all_columns(df)
    measure_opts = ms.ALL_MEASURES
    fmt_opts = list(ms.NUMBER_FORMAT_PRESETS.keys())

    with st.expander("🌐 Global measure & number format (applies to every KPI card at once)", expanded=False):
        st.caption("Each card keeps its own column and its own filters — this just bulk-sets the "
                   "measure and number format across all cards in one click.")
        gc1, gc2, gc3 = st.columns([2, 2, 1])
        with gc1:
            g_measure = st.selectbox("Measure", measure_opts, key=f"{key_prefix}measure")
        with gc2:
            g_fmt = st.selectbox("Number format", fmt_opts, key=f"{key_prefix}fmt")
        with gc3:
            st.write("")
            st.write("")
            if st.button("Apply to all cards", key=f"{key_prefix}apply", use_container_width=True):
                for card in cards:
                    card["measure"] = g_measure
                    card["number_format"] = g_fmt
                st.rerun()


# ==================================================================================
# CUSTOM CHART (Power-BI style field wells)
# ==================================================================================
def new_chart(df):
    all_cols = ms.list_all_columns(df)
    cats = [c["name"] for c in all_cols if c["kind"] in ("categorical", "text", "boolean")]
    nums = [c["name"] for c in all_cols if c["kind"] == "numeric"]
    return {
        "id": uuid.uuid4().hex[:8],
        "title": "New Chart",
        "type": "Bar",
        "x_col": cats[0] if cats else (all_cols[0]["name"] if all_cols else None),
        "x_grain": None,
        "y_col": nums[0] if nums else (all_cols[0]["name"] if all_cols else None),
        "y_measure": "Sum" if nums else "Count",
        "color_col": None,
        "filters": [],
        "pinned": False,
    }


def render_chart_editor(df, chart, key_prefix):
    all_cols = ms.list_all_columns(df)
    col_names = [c["name"] for c in all_cols]
    kind_by_name = {c["name"]: c["kind"] for c in all_cols}
    color_opts = ["(none)"] + [c["name"] for c in all_cols if c["kind"] in ("categorical", "text", "boolean")]

    chart["title"] = st.text_input("Chart title", chart["title"], key=f"{key_prefix}title_{chart['id']}")
    chart["type"] = st.selectbox("Chart type", CHART_TYPES,
                                  index=CHART_TYPES.index(chart["type"]) if chart["type"] in CHART_TYPES else 0,
                                  key=f"{key_prefix}type_{chart['id']}",
                                  format_func=lambda t: f"{CHART_ICONS.get(t,'')} {t}")

    st.markdown("**Field wells**")
    c1, c2, c3 = st.columns(3)
    with c1:
        chart["x_col"] = st.selectbox("X axis field", col_names,
                                       index=col_names.index(chart["x_col"]) if chart["x_col"] in col_names else 0,
                                       key=f"{key_prefix}x_{chart['id']}")
        if kind_by_name.get(chart["x_col"]) == "date":
            labels = [g[0] for g in DATE_GRAINS]
            codes = [g[1] for g in DATE_GRAINS]
            cur = codes.index(chart.get("x_grain")) if chart.get("x_grain") in codes else 0
            chart["x_grain"] = codes[labels.index(st.selectbox("Date granularity", labels, index=cur,
                                                                 key=f"{key_prefix}xg_{chart['id']}"))]
        else:
            chart["x_grain"] = None
    with c2:
        chart["y_col"] = st.selectbox("Y axis field", col_names,
                                       index=col_names.index(chart["y_col"]) if chart["y_col"] in col_names else 0,
                                       key=f"{key_prefix}y_{chart['id']}")
        y_opts = ms.measures_for_kind(kind_by_name.get(chart["y_col"], "text"))
        chart["y_measure"] = st.selectbox("Y measure", y_opts,
                                           index=y_opts.index(chart["y_measure"]) if chart["y_measure"] in y_opts else 0,
                                           key=f"{key_prefix}ym_{chart['id']}")
    with c3:
        cur_color = chart.get("color_col") or "(none)"
        chosen = st.selectbox("Color / Legend field (optional)", color_opts,
                               index=color_opts.index(cur_color) if cur_color in color_opts else 0,
                               key=f"{key_prefix}c_{chart['id']}")
        chart["color_col"] = None if chosen == "(none)" else chosen

    chart["filters"] = render_filter_builder(df, chart.get("filters", []), key_prefix=f"{key_prefix}chart_{chart['id']}_")


def _grouped(df, x_col, x_grain, y_col, y_measure, color_col):
    """Group the filtered dataframe by X (+ optional color) and aggregate Y with the chosen measure."""
    work = df.copy()
    x_key = x_col
    if x_grain and x_col in work.columns:
        work[x_col] = pd.to_datetime(work[x_col], errors="coerce")
        work = work.dropna(subset=[x_col])
        work[x_col] = work[x_col].dt.to_period({"D": "D", "W": "W", "ME": "M", "YE": "Y"}.get(x_grain, "D")).dt.start_time

    group_cols = [x_key] + ([color_col] if color_col else [])
    group_cols = [c for c in group_cols if c in work.columns]
    if not group_cols:
        return pd.DataFrame(), y_col

    agg_map = {
        "Sum": "sum", "Average": "mean", "Min": "min", "Max": "max", "Median": "median", "Std Dev": "std",
    }
    if y_measure == "Count":
        out = work.groupby(group_cols, dropna=True)[y_col].apply(lambda s: s.notna().sum()).reset_index(name=y_col)
    elif y_measure == "Distinct Count":
        out = work.groupby(group_cols, dropna=True)[y_col].nunique().reset_index(name=y_col)
    elif y_measure == "Count Blank":
        out = work.groupby(group_cols, dropna=True)[y_col].apply(lambda s: s.isna().sum()).reset_index(name=y_col)
    elif y_measure == "Most Frequent (Mode)":
        out = work.groupby(group_cols, dropna=True)[y_col].agg(
            lambda s: s.dropna().astype(str).value_counts().index[0] if s.dropna().shape[0] else "-").reset_index(name=y_col)
    elif y_measure in ("Earliest (Min)", "Latest (Max)"):
        work[y_col] = pd.to_datetime(work[y_col], errors="coerce")
        fn = "min" if y_measure == "Earliest (Min)" else "max"
        out = work.groupby(group_cols, dropna=True)[y_col].agg(fn).reset_index()
    else:
        work[y_col] = pd.to_numeric(work[y_col], errors="coerce")
        out = work.groupby(group_cols, dropna=True)[y_col].agg(agg_map.get(y_measure, "sum")).reset_index()

    return out.sort_values(y_col, ascending=False), y_col


def build_custom_figure(df, chart, style):
    """Generic figure builder driven entirely by the user's field-well choices."""
    palette = style.get("palette") or px.colors.qualitative.Set2
    template = style.get("template", "plotly_white")
    font_family = style.get("font_family", "Arial")
    font_color = style.get("font_color", "#1a1a1a")
    font_size = style.get("font_size", 13)
    show_legend = style.get("show_legend", True)
    show_labels = style.get("show_labels", True)

    fdf = ms.apply_filters(df, chart.get("filters", []))
    ctype = chart["type"]
    x_col, x_grain = chart["x_col"], chart.get("x_grain")
    y_col, y_measure = chart["y_col"], chart["y_measure"]
    color_col = chart.get("color_col")
    fig, insight, table_df = None, "", None

    try:
        if ctype == "Table":
            table_df = fdf.copy()
            insight = f"{len(table_df):,} rows after filters."
        elif ctype == "Scatter":
            sample = fdf if len(fdf) <= 5000 else fdf.sample(5000, random_state=42)
            fig = px.scatter(sample, x=x_col, y=y_col, color=color_col, color_discrete_sequence=palette, opacity=0.7)
            if pd.api.types.is_numeric_dtype(fdf.get(x_col, pd.Series(dtype=float))) and pd.api.types.is_numeric_dtype(fdf.get(y_col, pd.Series(dtype=float))):
                corr = fdf[[x_col, y_col]].dropna().corr().iloc[0, 1]
                insight = f"Correlation between {x_col} and {y_col} is {corr:.2f}."
            else:
                insight = f"Relationship between {x_col} and {y_col}."
        elif ctype == "Histogram":
            fig = px.histogram(fdf, x=y_col, color=color_col, nbins=30, color_discrete_sequence=palette, marginal="box")
            skew = pd.to_numeric(fdf[y_col], errors="coerce").dropna().skew() if y_col in fdf.columns else 0
            shape = "right-skewed" if skew > 0.5 else ("left-skewed" if skew < -0.5 else "fairly symmetric")
            insight = f"Distribution of {y_col} is {shape}."
        elif ctype == "Box":
            fig = px.box(fdf, x=x_col, y=y_col, color=color_col or x_col, color_discrete_sequence=palette)
            insight = f"Spread of {y_col} across {x_col}."
        elif ctype == "Heatmap":
            if color_col:
                pivot = fdf.pivot_table(index=x_col, columns=color_col, values=y_col,
                                         aggfunc={"Sum": "sum", "Average": "mean", "Count": "count",
                                                  "Min": "min", "Max": "max", "Median": "median"}.get(y_measure, "sum"))
                fig = go.Figure(data=go.Heatmap(z=pivot.values, x=[str(c) for c in pivot.columns],
                                                 y=[str(i) for i in pivot.index], colorscale="Blues",
                                                 text=np.round(pivot.values.astype(float), 1) if show_labels else None,
                                                 texttemplate="%{text}" if show_labels else None))
                insight = f"Darker = higher {y_measure} of {y_col}, split by {x_col} x {color_col}."
            else:
                g, val_col = _grouped(fdf, x_col, x_grain, y_col, y_measure, None)
                fig = go.Figure(data=go.Bar(x=g[x_col].astype(str), y=g[val_col], marker_color=palette[0]))
                insight = "Add a Color/Legend field to see a true heatmap matrix; showing a bar view meanwhile."
        elif ctype == "Treemap":
            path = [x_col] + ([color_col] if color_col else [])
            g, val_col = _grouped(fdf, x_col, x_grain, y_col, y_measure, color_col)
            g[val_col] = pd.to_numeric(g[val_col], errors="coerce").abs()
            fig = px.treemap(g, path=path, values=val_col, color_discrete_sequence=palette)
            insight = f"Box size = {y_measure} of {y_col}."
        else:
            g, val_col = _grouped(fdf, x_col, x_grain, y_col, y_measure, color_col)
            if len(g) > 30 and ctype in ("Bar", "Pie", "Donut"):
                g = g.sort_values(val_col, ascending=False).head(30)
            if ctype == "Bar":
                fig = px.bar(g, x=x_col, y=val_col, color=color_col, color_discrete_sequence=palette,
                             text=val_col if show_labels else None, barmode="group")
            elif ctype == "Line":
                fig = px.line(g.sort_values(x_col), x=x_col, y=val_col, color=color_col, markers=True,
                              color_discrete_sequence=palette)
            elif ctype == "Area":
                fig = px.area(g.sort_values(x_col), x=x_col, y=val_col, color=color_col, color_discrete_sequence=palette)
            elif ctype in ("Pie", "Donut"):
                fig = px.pie(g, names=x_col, values=val_col, color_discrete_sequence=palette,
                             hole=0.45 if ctype == "Donut" else 0.0)
                fig.update_traces(textinfo="label+percent" if show_labels else "none")
            if len(g) == 0:
                insight = "No data available with the current filters."
            elif ctype in ("Line", "Area") and x_grain:
                g_sorted = g.sort_values(x_col)
                yv = pd.to_numeric(g_sorted[val_col], errors="coerce").dropna().values.astype(float)
                if len(yv) >= 2:
                    slope = np.polyfit(np.arange(len(yv)), yv, 1)[0]
                    direction = "upward 📈" if slope > 0 else ("downward 📉" if slope < 0 else "flat ➡️")
                    insight = f"Overall trend of {y_col} is {direction} across the selected period."
                else:
                    insight = "Not enough time points to determine a trend."
            else:
                top = g.sort_values(val_col, ascending=False).iloc[0]
                total = pd.to_numeric(g[val_col], errors="coerce").sum()
                share = (top[val_col] / total * 100) if total else 0
                insight = f"'{top[x_col]}' leads with {ms.fmt_measure_value(top[val_col])} ({share:.1f}% share)."
    except Exception as e:
        fig = go.Figure()
        fig.add_annotation(text=f"Chart could not be built:\n{e}", showarrow=False, font=dict(size=13))
        insight = "Check that the chosen X/Y fields make sense together."

    if fig is not None:
        fig.update_layout(
            title=chart.get("title", ""),
            template=template,
            font=dict(family=font_family, size=font_size, color=font_color),
            showlegend=show_legend,
            margin=dict(l=30, r=30, t=60, b=30),
            paper_bgcolor=style.get("chart_bg", "rgba(0,0,0,0)"),
            plot_bgcolor=style.get("plot_bg", "rgba(0,0,0,0)"),
        )
        if ctype in ("Bar",):
            fig.update_traces(textposition="outside" if show_labels else None)

    return fig, insight, table_df
