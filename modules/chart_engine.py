"""
chart_engine.py
----------------
Generic chart-variant generator + Plotly figure builder.
For every chart "family" (Bar, Line, Pie, Comparison Bar, Area, Scatter, Box,
Histogram, Treemap, Heatmap) it looks at whatever columns exist in the current
dataframe and proposes up to 10 different, meaningful variants automatically -
no hard-coded column names anywhere.
"""

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# Shared config for every st.plotly_chart(...) call in the app. Zoom/pan/reset
# are kept ON (scrollZoom + the toolbar buttons) so crowded data labels can be
# zoomed into and read properly - only box/lasso select are removed since
# nothing in this app uses selection events. NOTE: mouse/scroll zoom here is a
# quick on-screen look only - it is NOT what syncs to the PDF (Plotly doesn't
# report live zoom state back to Streamlit). For a zoom level that also shows
# up identically in the exported PDF, use the explicit "🔍 Zoom" slider that
# app.py renders above each Boss Dashboard chart (see apply_zoom_window below).
PLOTLY_CONFIG = {
    "displaylogo": False,
    "displayModeBar": "hover",
    "scrollZoom": True,
    "modeBarButtonsToRemove": [
        "select2d", "lasso2d",
        "hoverClosestCartesian", "hoverCompareCartesian",
        "toggleSpikelines",
    ],
}


def apply_zoom_window(fig, start_pct: float = 0, end_pct: float = 100):
    """Restricts the figure's x-axis to the [start_pct, end_pct] window (0-100)
    of its own data range - i.e. an explicit, storable "zoom" that (unlike
    mouse-drag zoom) can be persisted and reproduced exactly, both on-screen
    AND in the PDF export (same fig object, same baked-in range). Spreads out
    the remaining points/bars so crowded data labels become readable. Safe
    no-op on any figure type this doesn't cleanly apply to (pie/treemap/heatmap,
    or a figure with no x data) - never raises."""
    try:
        if start_pct is None or end_pct is None:
            return fig
        start_pct = max(0, min(100, float(start_pct)))
        end_pct = max(0, min(100, float(end_pct)))
        if start_pct >= end_pct or (start_pct <= 0 and end_pct >= 100):
            return fig

        xs_all = []
        for tr in fig.data:
            xv = getattr(tr, "x", None)
            if xv is not None:
                xs_all.extend(list(xv))
        if len(xs_all) < 2:
            return fig

        # de-dup while preserving first-seen order (works for both category
        # axes and already-sorted numeric/date axes)
        seen, ordered = set(), []
        for v in xs_all:
            if v not in seen:
                seen.add(v)
                ordered.append(v)
        n = len(ordered)
        if n < 2:
            return fig

        start_i = int(n * start_pct / 100)
        end_i = min(n, max(start_i + 1, int(round(n * end_pct / 100))))
        first_val, last_val = ordered[start_i], ordered[end_i - 1]

        is_numeric = all(isinstance(v, (int, float, np.integer, np.floating)) and not isinstance(v, bool)
                         for v in ordered)
        is_datelike = False
        if not is_numeric:
            try:
                is_datelike = pd.api.types.is_datetime64_any_dtype(pd.Series(ordered))
            except Exception:
                is_datelike = False

        if is_numeric or is_datelike:
            fig.update_xaxes(range=[first_val, last_val], autorange=False)
        else:
            fig.update_xaxes(range=[start_i - 0.5, end_i - 1 + 0.5], autorange=False)
    except Exception:
        pass
    return fig

FAMILIES = ["Bar", "Line", "Pie", "Comparison", "Area", "Scatter", "Box", "Histogram", "Treemap", "Heatmap"]

DEFAULT_PALETTE = px.colors.qualitative.Set2


def _top_categories(df, col, measure, topn=8):
    g = df.groupby(col, dropna=True)[measure].sum().sort_values(ascending=False) if measure else \
        df[col].value_counts()
    top = g.head(topn)
    other = g.iloc[topn:].sum()
    if other > 0:
        top = pd.concat([top, pd.Series({"Other": other})])
    return top


def _agg(df, dims, measure, agg="sum"):
    if measure is None:
        out = df.groupby(dims, dropna=True).size().reset_index(name="Count")
        return out, "Count"
    out = df.groupby(dims, dropna=True)[measure].agg(agg).reset_index()
    return out, measure


def _insight_bar(g, dim, val_col):
    if len(g) == 0:
        return "No data available for this combination."
    g2 = g.sort_values(val_col, ascending=False)
    top = g2.iloc[0]
    total = g2[val_col].sum()
    share = (top[val_col] / total * 100) if total else 0
    return f"'{top[dim]}' leads with {top[val_col]:,.0f} ({share:.1f}% share of total)."


def _insight_trend(ts, val_col):
    if len(ts) < 2:
        return "Not enough time points to determine a trend."
    x = np.arange(len(ts))
    y = ts[val_col].values.astype(float)
    slope = np.polyfit(x, y, 1)[0] if len(ts) > 1 else 0
    direction = "upward 📈" if slope > 0 else ("downward 📉" if slope < 0 else "flat ➡️")
    return f"Overall trend is {direction} across the selected period."


# --------------------------------------------------------------------------------
# VARIANT GENERATORS (return up to 10 variant dicts per family)
# --------------------------------------------------------------------------------
def generate_variants(df, meta, family):
    cats = meta["categorical_cols"]
    nums = meta["numeric_cols"]
    dates = meta["date_cols"]
    primary_measure = meta["primary_measure"]
    variants = []

    if family == "Bar":
        measures = nums if nums else [None]
        for cat in cats:
            for m in ([primary_measure] if primary_measure else measures):
                variants.append({"dim": cat, "measure": m, "agg": "sum",
                                  "title": f"Total {m or 'Records'} by {cat}"})
                if len(variants) >= 10:
                    break
            if len(variants) >= 10:
                break
        # fill remaining slots by varying aggregation / secondary measures
        if len(variants) < 10:
            for cat in cats:
                for m in nums:
                    if m == primary_measure:
                        continue
                    variants.append({"dim": cat, "measure": m, "agg": "mean",
                                      "title": f"Average {m} by {cat}"})
                    if len(variants) >= 10:
                        break
                if len(variants) >= 10:
                    break

    elif family == "Line":
        dcol = meta["primary_date"]
        if dcol:
            measures = [primary_measure] + [m for m in nums if m != primary_measure]
            grans = [("D", "Daily"), ("W", "Weekly"), ("ME", "Monthly")]
            for m in measures:
                if m is None:
                    continue
                for code, label in grans:
                    variants.append({"date_col": dcol, "measure": m, "gran": code,
                                      "title": f"{label} Trend of {m}"})
                    if len(variants) >= 6:
                        break
                if len(variants) >= 6:
                    break
            # split-by-category trend variants
            for cat in cats[:4]:
                variants.append({"date_col": dcol, "measure": primary_measure or nums[0] if nums else None,
                                  "gran": "W", "split_by": cat,
                                  "title": f"Weekly Trend of {primary_measure or 'Records'} by {cat}"})
                if len(variants) >= 10:
                    break
        else:
            variants.append({"date_col": None, "measure": primary_measure, "gran": None,
                              "title": f"Sequential Trend of {primary_measure or 'Records'} (no date column found)"})

    elif family == "Pie":
        measures = [primary_measure] if primary_measure else [None]
        for cat in cats:
            for m in measures:
                variants.append({"dim": cat, "measure": m,
                                  "title": f"Share of {m or 'Records'} by {cat}"})
                if len(variants) >= 10:
                    break
            if len(variants) >= 10:
                break
        if len(variants) < 10:
            for cat in cats:
                for m in nums:
                    if m == primary_measure:
                        continue
                    variants.append({"dim": cat, "measure": m, "title": f"Share of {m} by {cat}"})
                    if len(variants) >= 10:
                        break
                if len(variants) >= 10:
                    break

    elif family == "Comparison":
        for i, c1 in enumerate(cats):
            for c2 in cats[i + 1:]:
                variants.append({"dim1": c1, "dim2": c2, "measure": primary_measure,
                                  "title": f"{primary_measure or 'Records'}: {c1} vs {c2}"})
                if len(variants) >= 10:
                    break
            if len(variants) >= 10:
                break

    elif family == "Area":
        dcol = meta["primary_date"]
        if dcol:
            measures = [primary_measure] + [m for m in nums if m != primary_measure]
            for m in measures:
                if m is None:
                    continue
                variants.append({"date_col": dcol, "measure": m, "gran": "D", "cum": False,
                                  "title": f"Daily {m} Area"})
                variants.append({"date_col": dcol, "measure": m, "gran": "D", "cum": True,
                                  "title": f"Cumulative {m} Over Time"})
                if len(variants) >= 6:
                    break
            for cat in cats[:4]:
                variants.append({"date_col": dcol, "measure": primary_measure, "gran": "W",
                                  "split_by": cat, "cum": False,
                                  "title": f"Weekly {primary_measure or 'Records'} Area by {cat}"})
                if len(variants) >= 10:
                    break
        else:
            variants.append({"date_col": None, "measure": primary_measure, "gran": None, "cum": True,
                              "title": "Cumulative Area (no date column found, using row order)"})

    elif family == "Scatter":
        if len(nums) >= 2:
            for i, m1 in enumerate(nums):
                for m2 in nums[i + 1:]:
                    color = cats[0] if cats else None
                    variants.append({"x": m1, "y": m2, "color": color,
                                      "title": f"{m1} vs {m2}" + (f" by {color}" if color else "")})
                    if len(variants) >= 10:
                        break
                if len(variants) >= 10:
                    break
        else:
            for cat in cats:
                variants.append({"x": cat, "y": primary_measure, "color": None,
                                  "title": f"{primary_measure or 'Records'} by {cat} (bubble)"})
                if len(variants) >= 10:
                    break

    elif family == "Box":
        measures = [primary_measure] + [m for m in nums if m != primary_measure]
        for cat in cats:
            for m in measures:
                if m is None:
                    continue
                variants.append({"dim": cat, "measure": m, "title": f"Distribution of {m} across {cat}"})
                if len(variants) >= 10:
                    break
            if len(variants) >= 10:
                break

    elif family == "Histogram":
        measures = [primary_measure] + [m for m in nums if m != primary_measure]
        bins_opts = [20, 40]
        for m in measures:
            if m is None:
                continue
            for b in bins_opts:
                variants.append({"measure": m, "bins": b, "title": f"Distribution of {m} ({b} bins)"})
                if len(variants) >= 6:
                    break
            if len(variants) >= 6:
                break
        for cat in cats[:4]:
            if primary_measure:
                variants.append({"measure": primary_measure, "bins": 30, "facet": cat,
                                  "title": f"Distribution of {primary_measure} split by {cat}"})
            if len(variants) >= 10:
                break

    elif family == "Treemap":
        for cat in cats:
            variants.append({"path": [cat], "measure": primary_measure,
                              "title": f"{primary_measure or 'Records'} Share Treemap - {cat}"})
            if len(variants) >= 10:
                break
        if len(variants) < 10:
            for i, c1 in enumerate(cats):
                for c2 in cats[i + 1:]:
                    variants.append({"path": [c1, c2], "measure": primary_measure,
                                      "title": f"{primary_measure or 'Records'} Treemap - {c1} > {c2}"})
                    if len(variants) >= 10:
                        break
                if len(variants) >= 10:
                    break

    elif family == "Heatmap":
        if len(nums) >= 2:
            variants.append({"kind": "corr", "cols": nums[:12], "title": "Correlation Heatmap (numeric columns)"})
        for i, c1 in enumerate(cats):
            for c2 in cats[i + 1:]:
                variants.append({"kind": "cross", "dim1": c1, "dim2": c2, "measure": primary_measure,
                                  "title": f"{primary_measure or 'Records'} Heatmap: {c1} x {c2}"})
                if len(variants) >= 10:
                    break
            if len(variants) >= 10:
                break

    # attach ids + family + placeholder insight
    for i, v in enumerate(variants):
        v["id"] = f"{family}_{i}"
        v["family"] = family
        v.setdefault("insight", "")
    return variants[:10]


# --------------------------------------------------------------------------------
# FIGURE BUILDER
# --------------------------------------------------------------------------------
def build_figure(df, variant, style):
    family = variant["family"]
    palette = style.get("palette") or DEFAULT_PALETTE
    template = style.get("template", "plotly_white")
    font_family = style.get("font_family", "Arial")
    font_color = style.get("font_color", "#1a1a1a")
    font_size = style.get("font_size", 13)
    show_legend = style.get("show_legend", True)
    show_labels = style.get("show_labels", True)
    title_text = style.get("title", variant.get("title", ""))

    fig = None
    insight = ""

    try:
        if family == "Bar":
            dim, measure, agg = variant["dim"], variant.get("measure"), variant.get("agg", "sum")
            top = _top_categories(df, dim, measure, topn=12) if measure else df[dim].value_counts().head(12)
            g = top.reset_index()
            g.columns = [dim, measure or "Count"]
            val_col = measure or "Count"
            fig = px.bar(g, x=dim, y=val_col, color=dim, color_discrete_sequence=palette,
                         text=val_col if show_labels else None)
            insight = _insight_bar(g, dim, val_col)

        elif family == "Line":
            dcol, measure, gran = variant.get("date_col"), variant["measure"], variant.get("gran")
            if dcol:
                tmp = df[[dcol, measure]].dropna()
                tmp[dcol] = pd.to_datetime(tmp[dcol], errors="coerce")
                tmp = tmp.dropna(subset=[dcol])
                split_by = variant.get("split_by")
                if split_by:
                    tmp[split_by] = df.loc[tmp.index, split_by]
                    top_cats = tmp[split_by].value_counts().head(5).index
                    tmp = tmp[tmp[split_by].isin(top_cats)]
                    ts = tmp.groupby([pd.Grouper(key=dcol, freq=gran), split_by])[measure].sum().reset_index()
                    fig = px.line(ts, x=dcol, y=measure, color=split_by, markers=True,
                                  color_discrete_sequence=palette)
                    insight = _insight_trend(ts.groupby(dcol)[measure].sum().reset_index(), measure)
                else:
                    ts = tmp.groupby(pd.Grouper(key=dcol, freq=gran))[measure].sum().reset_index()
                    fig = px.line(ts, x=dcol, y=measure, markers=True, color_discrete_sequence=palette)
                    insight = _insight_trend(ts, measure)
            else:
                tmp = df[[measure]].dropna().reset_index()
                fig = px.line(tmp, x="index", y=measure, markers=True, color_discrete_sequence=palette)
                insight = _insight_trend(tmp.rename(columns={"index": "x"}), measure)

        elif family == "Pie":
            dim, measure = variant["dim"], variant.get("measure")
            top = _top_categories(df, dim, measure, topn=8)
            g = top.reset_index()
            g.columns = [dim, measure or "Count"]
            val_col = measure or "Count"
            fig = px.pie(g, names=dim, values=val_col, color_discrete_sequence=palette, hole=0.35)
            try:
                fig.update_traces(textinfo="label+percent" if show_labels else "none",
                                   selector=dict(type="pie"))
            except Exception:
                pass
            insight = _insight_bar(g, dim, val_col)

        elif family == "Comparison":
            dim1, dim2, measure = variant["dim1"], variant["dim2"], variant.get("measure")
            out, val_col = _agg(df, [dim1, dim2], measure)
            top_d1 = out.groupby(dim1)[val_col].sum().sort_values(ascending=False).head(8).index
            out = out[out[dim1].isin(top_d1)]
            fig = px.bar(out, x=dim1, y=val_col, color=dim2, barmode="group",
                         color_discrete_sequence=palette, text=val_col if show_labels else None)
            insight = _insight_bar(out.groupby(dim1)[val_col].sum().reset_index(), dim1, val_col)

        elif family == "Area":
            dcol, measure, gran, cum = variant.get("date_col"), variant["measure"], variant.get("gran"), variant.get("cum", False)
            if dcol:
                tmp = df[[dcol, measure]].dropna()
                tmp[dcol] = pd.to_datetime(tmp[dcol], errors="coerce")
                tmp = tmp.dropna(subset=[dcol])
                split_by = variant.get("split_by")
                if split_by:
                    tmp[split_by] = df.loc[tmp.index, split_by]
                    top_cats = tmp[split_by].value_counts().head(5).index
                    tmp = tmp[tmp[split_by].isin(top_cats)]
                    ts = tmp.groupby([pd.Grouper(key=dcol, freq=gran), split_by])[measure].sum().reset_index()
                    if cum:
                        ts[measure] = ts.groupby(split_by)[measure].cumsum()
                    fig = px.area(ts, x=dcol, y=measure, color=split_by, color_discrete_sequence=palette)
                else:
                    ts = tmp.groupby(pd.Grouper(key=dcol, freq=gran))[measure].sum().reset_index()
                    if cum:
                        ts[measure] = ts[measure].cumsum()
                    fig = px.area(ts, x=dcol, y=measure, color_discrete_sequence=palette)
                insight = _insight_trend(ts if not variant.get("split_by") else ts.groupby(dcol)[measure].sum().reset_index(), measure)
            else:
                tmp = df[[measure]].dropna().reset_index()
                if cum:
                    tmp[measure] = tmp[measure].cumsum()
                fig = px.area(tmp, x="index", y=measure, color_discrete_sequence=palette)
                insight = "Cumulative view across all rows (no date column detected)."

        elif family == "Scatter":
            x, y, color = variant["x"], variant["y"], variant.get("color")
            sample = df if len(df) <= 5000 else df.sample(5000, random_state=42)
            fig = px.scatter(sample, x=x, y=y, color=color, color_discrete_sequence=palette, opacity=0.7)
            corr = df[[x, y]].dropna().corr().iloc[0, 1] if x in df.columns and y in df.columns and pd.api.types.is_numeric_dtype(df[x]) and pd.api.types.is_numeric_dtype(df[y]) else None
            insight = f"Correlation between {x} and {y} is {corr:.2f}." if corr is not None else "Relationship between selected fields."

        elif family == "Box":
            dim, measure = variant["dim"], variant["measure"]
            top_cats = df[dim].value_counts().head(10).index
            sub = df[df[dim].isin(top_cats)]
            fig = px.box(sub, x=dim, y=measure, color=dim, color_discrete_sequence=palette)
            med = sub.groupby(dim)[measure].median().sort_values(ascending=False)
            insight = f"Highest median {measure} is in '{med.index[0]}' ({med.iloc[0]:,.1f})." if len(med) else ""

        elif family == "Histogram":
            measure, bins = variant["measure"], variant.get("bins", 30)
            facet = variant.get("facet")
            fig = px.histogram(df, x=measure, nbins=bins, color=facet, color_discrete_sequence=palette,
                                marginal="box")
            skew = df[measure].dropna().skew() if measure in df.columns else 0
            shape = "right-skewed (long tail of high values)" if skew > 0.5 else ("left-skewed" if skew < -0.5 else "fairly symmetric")
            insight = f"Distribution of {measure} is {shape}."

        elif family == "Treemap":
            path, measure = variant["path"], variant.get("measure")
            out, val_col = _agg(df, path, measure)
            fig = px.treemap(out, path=path, values=val_col, color_discrete_sequence=palette)
            insight = f"Larger boxes = higher {val_col}. Top-level split by {path[0]}."

        elif family == "Heatmap":
            if variant.get("kind") == "corr":
                cols = [c for c in variant["cols"] if c in df.columns]
                corr = df[cols].corr()
                fig = go.Figure(data=go.Heatmap(z=corr.values, x=corr.columns, y=corr.columns,
                                                 colorscale="RdBu", zmid=0,
                                                 text=np.round(corr.values, 2) if show_labels else None,
                                                 texttemplate="%{text}" if show_labels else None))
                strongest = corr.where(~np.eye(len(corr), dtype=bool)).abs().stack().sort_values(ascending=False)
                if len(strongest):
                    a, b = strongest.index[0]
                    insight = f"Strongest relationship: {a} & {b} (corr={corr.loc[a,b]:.2f})."
            else:
                dim1, dim2, measure = variant["dim1"], variant["dim2"], variant.get("measure")
                pivot = df.pivot_table(index=dim1, columns=dim2, values=measure, aggfunc="sum") if measure else \
                    pd.crosstab(df[dim1], df[dim2])
                fig = go.Figure(data=go.Heatmap(z=pivot.values, x=[str(c) for c in pivot.columns],
                                                 y=[str(i) for i in pivot.index], colorscale="Blues",
                                                 text=np.round(pivot.values, 1) if show_labels else None,
                                                 texttemplate="%{text}" if show_labels else None))
                insight = f"Darker cells = higher {measure or 'count'} for that {dim1} x {dim2} combination."
    except Exception as e:
        fig = go.Figure()
        fig.add_annotation(text=f"Chart could not be built with current filters:\n{e}",
                            showarrow=False, font=dict(size=13))
        insight = "This variant is not available with the current filter selection."

    if fig is not None:
        fig.update_layout(
            title=title_text,
            template=template,
            font=dict(family=font_family, size=font_size, color=font_color),
            showlegend=show_legend,
            margin=dict(l=30, r=30, t=60, b=30),
            paper_bgcolor=style.get("chart_bg", "rgba(0,0,0,0)"),
            plot_bgcolor=style.get("plot_bg", "rgba(0,0,0,0)"),
        )
        # NOTE: every update_traces() call below is wrapped in its own try/except.
        # These calls only add cosmetic styling (data-label text) on top of a chart
        # that already built successfully above - they should never be able to take
        # the whole page down. Before this fix, an update_traces() call with no
        # `selector` applies its kwargs to EVERY trace in the figure; if the
        # installed Plotly version (unpinned in requirements.txt, so Streamlit
        # Cloud can silently pull a newer release on every redeploy) rejects one of
        # those kwargs for a trace it produced, the ValueError propagated all the
        # way up and crashed the entire chart tab. Now we scope the update to the
        # trace type it's meant for and swallow (log-free, silent) any styling
        # failure so the chart still renders - just without that particular touch.
        if family == "Bar" or family == "Comparison":
            try:
                fig.update_traces(textposition="outside" if show_labels else None,
                                   selector=dict(type="bar"))
            except Exception:
                pass
        if family in ("Line", "Area"):
            # Without this, values only ever show up on hover - the whole point of
            # "show data labels" is that they're visible on the chart itself, with
            # or without the cursor on it.
            try:
                if show_labels:
                    fig.update_traces(mode="lines+markers+text", texttemplate="%{y:,.2s}",
                                       textposition="top center", cliponaxis=False,
                                       selector=dict(type="scatter"))
                else:
                    fig.update_traces(mode="lines+markers", selector=dict(type="scatter"))
            except Exception:
                # Fall back to a plain line/area with no text labels rather than
                # blow up the whole page - the chart itself is still valid.
                try:
                    fig.update_traces(mode="lines+markers", selector=dict(type="scatter"))
                except Exception:
                    pass

    variant["insight"] = insight
    return fig, insight
