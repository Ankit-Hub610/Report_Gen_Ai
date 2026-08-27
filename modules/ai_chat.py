"""
ai_chat.py
----------
Free, natural-language "Chat with your Data" assistant.

TWO free providers are supported, auto-detected from the key's shape:
  - Google Gemini (key starts with "AIza..." or the newer "AQ....") — PRIMARY/recommended. A fixed,
    genuinely strong general-purpose model (gemini-2.0-flash) with a
    generous free tier, no card required. Being a fixed model (not an
    auto-router that swaps between whatever's free that week) is what makes
    its answers consistent — this is what fixed the reported "general
    questions get different/wrong answers every time" complaint, which was
    traced to OpenRouter's free auto-router silently landing on a different,
    often much weaker, model from one call to the next.
  - OpenRouter (key starts with "sk-or-..." or set via OPENROUTER_API_KEY) —
    kept as a fallback/alternative for anyone who already has that key, or
    if Gemini's free tier is ever rate-limited. Uses the model id
    "openrouter/free", OpenRouter's own auto-router.

NOTE (why not Groq): Groq's free API blocks requests coming from
datacenter/cloud-hosted IPs (its own anti-abuse policy) — so it works when
you run the app on your own laptop, but fails with a 403 the moment the app
is deployed to Streamlit Community Cloud, Render, or any other cloud host.
Neither Gemini nor OpenRouter has that restriction.

HOW IT ANSWERS ACCURATELY (not just guessing from memory):
The model is given a "run_sql" tool. For anything that needs real numbers
(a specific record, a trend over the last N months, a comparison, etc.)
the model writes a read-only SQL query, we run it through the SAME sandboxed
DuckDB engine already used by the SQL Query tab (modules/query_engine.py —
read-only, no writes, no file access), and feed the actual result back to
the model so its final answer is grounded in the real data instead of
hallucinated. The SQL + result table are also returned so the UI can show
them as "proof" under the chat answer.

PAST / PRESENT / FUTURE, all in one place:
The system prompt now includes, whenever a dataset is loaded: the raw
dataset shape (for "past" queries via run_sql), the CURRENT KPI cards
(present state, including the month-over-month trend delta), and the
pre-computed regression forecast from intel_engine.compute_forecast() for
the next few periods (future) — all real, pre-calculated numbers, not the
model's own guess, so "what will happen next" answers are grounded exactly
like "what happened before" ones.

WORKS WITHOUT ANY DATA LOADED, TOO:
When no dataset is loaded yet, the assistant still answers general
questions (like a normal AI chat) — it just skips the dataset/KPI/forecast
sections of the prompt and doesn't offer the run_sql tool, since there's no
table to query. The AI Assistant page no longer hard-stops until data is
loaded — see app.py's "🤖 AI Assistant" page.

SETUP (one-time, free):
  1. Go to https://aistudio.google.com -> sign in with any Google account ->
     Get API key -> Create API key -> copy it (starts with "AIza..." or "AQ....").
     (Alternative: https://openrouter.ai -> Keys -> Create key, starts with
     "sk-or-v1-...".)
  2. Either:
       a) set environment variable GEMINI_API_KEY (or OPENROUTER_API_KEY)
          before running streamlit, OR
       b) create .streamlit/secrets.toml with:  GEMINI_API_KEY = "AIza..."
     If neither is set, only an admin login will see a place to paste the key
     for that session (kept only in memory, never written to disk) — regular
     client/viewer logins never see a key field at all.
"""

import os
import json

import pandas as pd
import requests

from modules import query_engine as qe

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL = "openrouter/free"   # auto-router: picks a live free, tool-capable model
GEMINI_MODEL = "gemini-2.0-flash"      # fixed model — see module docstring for why that matters
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
MAX_TOOL_ROUNDS = 3
MAX_ROWS_TO_MODEL = 40   # cap how many result rows get sent back into the prompt (keeps cost/latency low)


class ChatError(Exception):
    pass


def detect_provider(api_key: str) -> str:
    """Keys need no manual provider dropdown — the shape says which service
    they're for: Gemini keys start with 'AIza' (legacy format) or 'AQ.'
    (Google's newer "Auth Key" format, rolled out through 2026 — same
    Gemini API, just a different-looking key). OpenRouter keys always
    start with 'sk-or-'. Defaults to 'openrouter' for anything else typed
    in (safest guess for a key of unrecognised shape)."""
    if api_key and (api_key.startswith("AIza") or api_key.startswith("AQ.")):
        return "gemini"
    return "openrouter"


def get_api_key():
    """Looks for a key the admin already configured (env var or Streamlit
    secrets — Gemini checked first, it's the recommended default) before
    falling back to whatever the admin typed into the UI this session."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
        if "GEMINI_API_KEY" in st.secrets:
            return st.secrets["GEMINI_API_KEY"]
        if "OPENROUTER_API_KEY" in st.secrets:
            return st.secrets["OPENROUTER_API_KEY"]
    except Exception:
        pass
    return None


def _dataset_summary(df: pd.DataFrame, meta: dict) -> str:
    lines = [f"Table name for SQL: {qe.DEFAULT_TABLE_NAME}", f"Rows: {len(df):,}", "Columns (name: dtype):"]
    for col in df.columns:
        lines.append(f"  - {col}: {df[col].dtype}")
    if meta.get("primary_date"):
        lines.append(f"Primary date column: {meta['primary_date']}")
    if meta.get("primary_measure"):
        lines.append(f"Primary numeric measure column: {meta['primary_measure']}")
    return "\n".join(lines)


def _kpi_summary(kpis: list) -> str:
    if not kpis:
        return "(no KPIs computed yet)"
    lines = []
    for k in kpis:
        line = f"  - {k['label']}: {k['value']} ({k.get('sub','')})"
        if k.get("delta"):
            line += f" — trend: {k['delta']}"
        lines.append(line)
    return "\n".join(lines)


def _dashboard_summary(dashboard_charts: list) -> str:
    if not dashboard_charts:
        return "(no charts pinned to the Boss Dashboard yet)"
    out = []
    for c in dashboard_charts:
        v = c["variant"]
        out.append(f"  - [{c['family']}] {v.get('title', v.get('id'))}")
    return "\n".join(out)


def _forecast_summary(forecast: dict) -> str:
    """Formats intel_engine.compute_forecast()'s output for the prompt — the
    "future" side of past/present/future. Always real numbers from an actual
    regression, never the model's own guess (see module docstring)."""
    if not forecast or not forecast.get("available"):
        reason = (forecast or {}).get("reason", "Not enough date/revenue history to forecast yet.")
        return f"(no forecast available — {reason})"
    lines = [f"Method: {forecast['method']} (R² = {forecast['r2']}, confidence: {forecast['confidence']}, "
             f"overall direction: {forecast['direction']})"]
    for p, v in zip(forecast["forecast_periods"], forecast["forecast_values"]):
        lines.append(f"  - {p} (projected): {v:,.2f}")
    return "\n".join(lines)


def _trend_summary(trend: dict) -> str:
    """Formats intel_engine.compute_trend_growth()'s output — the "past"
    story behind the present KPI numbers (best/worst month, overall % change,
    CAGR), all pre-computed, real."""
    if not trend or not trend.get("available"):
        reason = (trend or {}).get("reason", "Not enough month-over-month history yet.")
        return f"(no trend history available — {reason})"
    lines = [f"Best month: {trend['best_period']} ({trend['best_period_value']:,.2f}), "
             f"Worst month: {trend['worst_period']} ({trend['worst_period_value']:,.2f})",
             f"Overall change (first month → last month): {trend.get('overall_change_pct')}%"]
    if trend.get("cagr_pct") is not None:
        lines.append(f"CAGR: {trend['cagr_pct']}%")
    return "\n".join(lines)


def _system_prompt(df, meta, kpis, dashboard_charts, trend=None, forecast=None):
    has_data = df is not None
    if not has_data:
        return """You are a helpful, general-purpose AI assistant embedded in a BI dashboard app.
No dataset is loaded in this session right now, so just answer the user's question directly and
helpfully from your own knowledge — general knowledge, explanations, advice, definitions, how-to
help, casual conversation, sports/news/trivia, writing help, math, coding help, translations, etc.
— exactly like a normal general-purpose AI assistant (e.g. ChatGPT) would.

If the user asks something that clearly needs THEIR OWN business data (e.g. "what was my revenue
last month"), gently let them know you don't see any dataset loaded yet and they can load one on
the 📥 Connect Data page — then answer whatever you still reasonably can in general terms.

Reply in clear, simple language (Hindi/English mix is fine if the user writes that way). Keep
answers concise — a short paragraph or a few bullet points, not an essay."""

    return f"""You are a helpful AI assistant embedded in a BI dashboard app. You can do TWO kinds
of things, and you should figure out which one a question needs:

1. QUESTIONS ABOUT THE USER'S OWN LOADED DATASET (their business data, KPIs, charts, trend history,
   or forecast) — for these, ground every number either in the PRE-COMPUTED PAST TREND / FUTURE
   FORECAST facts given to you below, or, for anything not already covered there, in a real
   run_sql result. Never guess or make up numbers for the user's data.
2. ANYTHING ELSE — general knowledge, explanations, advice, definitions, how-to help, casual
   conversation, sports/news/trivia, writing help, math, coding help, translations, etc. — answer
   these directly and helpfully from your own knowledge, exactly like a normal general-purpose AI
   assistant (e.g. ChatGPT) would. Do NOT refuse or deflect a question just because it isn't about
   the loaded dataset — only the DATASET-related numbers need grounding; your general knowledge
   answers don't need a tool call at all.

Reply in clear, simple language (Hindi/English mix is fine if the user writes that way).

DATASET (use run_sql for anything specific/historical not already summarised below — this is your
"PAST" source of truth: individual records, custom date ranges, breakdowns, etc.)
{_dataset_summary(df, meta)}

PRESENT — CURRENT KPI CARDS ON THE DASHBOARD (includes month-over-month trend where available)
{_kpi_summary(kpis)}

PAST — TREND HISTORY (pre-computed, real — best/worst month, overall change, CAGR)
{_trend_summary(trend)}

FUTURE — FORECAST (pre-computed via linear regression on monthly history — ALWAYS label this as a
projection, never state it as a certainty, and mention the confidence level given)
{_forecast_summary(forecast)}

CHARTS ALREADY PINNED TO THE BOSS DASHBOARD
{_dashboard_summary(dashboard_charts)}

TOOL AVAILABLE: run_sql(query)
- Runs a single read-only SQL SELECT (DuckDB syntax) against the table `{qe.DEFAULT_TABLE_NAME}` and
  returns the result rows.
- Use it for anything specific about the user's loaded dataset that ISN'T already answered by the
  PRESENT/PAST/FUTURE sections above — e.g. "which record is number 5", a custom date range, a
  breakdown by a specific column, etc.
  e.g. "which record is number 5" -> SELECT * FROM {qe.DEFAULT_TABLE_NAME} LIMIT 1 OFFSET 4;
- For anything NOT about this dataset, don't call this tool at all — just answer normally.
- Only SELECT/WITH is allowed. No INSERT/UPDATE/DELETE/DDL.
- You may call it more than once if the first query needs refining.

WHEN YOU ANSWER A DATASET QUESTION
- Ground every number in the facts given above, or a run_sql result — don't estimate.
- Explain the finding in plain language first, then the supporting number(s).
- A "what's happening / what happened / what's coming next" style question should draw on
  PRESENT + PAST + FUTURE together where relevant, not just one of them.
- When it's useful, suggest which chart type would visualise this well and which columns to put
  on which axis (e.g. "a Line chart with {meta.get('primary_date','the date column')} on X and
  the totals on Y would show this trend clearly").

WHEN YOU ANSWER A GENERAL QUESTION
- Just answer it well and concisely, like any capable AI assistant would — no need to mention the
  dataset, SQL, or the dashboard at all unless the user's question is actually about those.

Keep answers concise — a short paragraph or a few bullet points, not an essay — for either kind.
"""


TOOLS = [{
    "type": "function",
    "function": {
        "name": "run_sql",
        "description": "Run a read-only SQL SELECT query against the loaded dataset and return the results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single read-only SQL SELECT/WITH statement."}
            },
            "required": ["query"],
        },
    },
}]

# Same tool, expressed in Gemini's function-declaration shape (no "type": "function" wrapper,
# and no JSON-Schema $-prefixed keys beyond what Gemini's subset supports — this subset is fine).
GEMINI_TOOLS = [{
    "function_declarations": [{
        "name": "run_sql",
        "description": "Run a read-only SQL SELECT query against the loaded dataset and return the results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A single read-only SQL SELECT/WITH statement."}
            },
            "required": ["query"],
        },
    }]
}]


def _call_gemini(api_key, system_instruction, contents, tools=None, temperature=0.2):
    """Low-level call to Gemini's native generateContent endpoint. `contents`
    is already in Gemini's {"role": "user"|"model", "parts": [...]} shape —
    callers build that, this just sends it and checks for errors."""
    body = {"contents": contents, "generationConfig": {"temperature": temperature}}
    if system_instruction:
        body["system_instruction"] = {"parts": [{"text": system_instruction}]}
    if tools:
        body["tools"] = tools
    resp = requests.post(
        GEMINI_URL,
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json=body,
        timeout=60,
    )
    if resp.status_code in (401, 403):
        raise ChatError("Gemini API key rejected — check the key at aistudio.google.com "
                         "(Get API key). Make sure it was pasted in full, with no extra spaces.")
    if resp.status_code == 429:
        raise ChatError("Gemini free-tier rate limit hit — wait a minute and try again.")
    if resp.status_code >= 400:
        raise ChatError(f"Gemini API error ({resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    if not data.get("candidates"):
        block_reason = (data.get("promptFeedback") or {}).get("blockReason")
        if block_reason:
            raise ChatError(f"Gemini declined to answer that (reason: {block_reason}). Try rephrasing.")
        raise ChatError("Gemini returned no answer — try again.")
    return data


def _gemini_extract(data):
    """Pulls (text, function_call, raw_model_content) out of a generateContent
    response. function_call is {"name": ..., "args": {...}} or None."""
    content = data["candidates"][0].get("content", {"role": "model", "parts": []})
    text_parts, fn_call = [], None
    for p in content.get("parts", []):
        if "text" in p:
            text_parts.append(p["text"])
        elif "functionCall" in p:
            fn_call = p["functionCall"]
    return "\n".join(text_parts).strip(), fn_call, content


def _call_openrouter(api_key, messages):
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter-recommended headers (used for free-tier rankings, not required to work)
            "HTTP-Referer": "https://sports-analytics-platform.local",
            "X-Title": "Sports Analytics Platform",
        },
        json={"model": OPENROUTER_MODEL, "messages": messages, "tools": TOOLS,
              "tool_choice": "auto", "temperature": 0.2},
        timeout=60,
    )
    if resp.status_code == 401:
        raise ChatError("OpenRouter API key rejected — check the key at openrouter.ai/keys.")
    if resp.status_code == 429:
        raise ChatError("Free-tier rate limit hit — wait a minute and try again.")
    if resp.status_code >= 400:
        raise ChatError(f"OpenRouter API error ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def ask(question, df, meta, kpis, dashboard_charts, api_key, history=None):
    """Runs the tool-calling loop. Returns dict:
       {answer: str, sql_used: str|None, proof_df: DataFrame|None, error: str|None}
    Routes to Gemini or OpenRouter based on the key's shape (see detect_provider) —
    this is the one place that decides, everything downstream is provider-specific."""
    if not api_key:
        return {"answer": None, "sql_used": None, "proof_df": None,
                "error": "No AI API key configured yet — add one below (it's free)."}

    system_prompt = _system_prompt(df, meta, kpis, dashboard_charts)
    if detect_provider(api_key) == "gemini":
        return _ask_gemini(question, df, system_prompt, api_key, history)
    return _ask_openrouter(question, df, system_prompt, api_key, history)


def _ask_openrouter(question, df, system_prompt, api_key, history):
    messages = [{"role": "system", "content": system_prompt}]
    for turn in (history or [])[-6:]:   # keep a little chat memory, capped to avoid huge prompts
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    last_sql, last_proof = None, None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            data = _call_openrouter(api_key, messages)
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")

            if not tool_calls:
                return {"answer": choice.get("content", "").strip(), "sql_used": last_sql,
                        "proof_df": last_proof, "error": None}

            messages.append(choice)
            for tc in tool_calls:
                args = json.loads(tc["function"]["arguments"] or "{}")
                sql = args.get("query", "")
                try:
                    result_df = qe.run_sql(df, sql)
                    last_sql, last_proof = sql, result_df
                    preview = result_df.head(MAX_ROWS_TO_MODEL).to_csv(index=False)
                    tool_content = f"{len(result_df)} row(s) returned.\n{preview}"
                except qe.QueryError as e:
                    tool_content = f"SQL error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": tool_content})

        return {"answer": "Ran out of steps trying to answer that — try rephrasing the question.",
                "sql_used": last_sql, "proof_df": last_proof, "error": None}
    except ChatError as e:
        return {"answer": None, "sql_used": last_sql, "proof_df": last_proof, "error": str(e)}
    except requests.RequestException as e:
        return {"answer": None, "sql_used": last_sql, "proof_df": last_proof,
                "error": f"Network error reaching OpenRouter — check internet: {e}"}


def _ask_gemini(question, df, system_prompt, api_key, history):
    contents = []
    for turn in (history or [])[-6:]:
        role = "model" if turn["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": question}]})

    tools = GEMINI_TOOLS if df is not None else None
    last_sql, last_proof = None, None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            data = _call_gemini(api_key, system_prompt, contents, tools=tools, temperature=0.2)
            text, fn_call, model_content = _gemini_extract(data)

            if not fn_call:
                return {"answer": text, "sql_used": last_sql, "proof_df": last_proof, "error": None}

            contents.append(model_content)   # the model's turn, including its functionCall part
            sql = (fn_call.get("args") or {}).get("query", "")
            try:
                result_df = qe.run_sql(df, sql)
                last_sql, last_proof = sql, result_df
                preview = result_df.head(MAX_ROWS_TO_MODEL).to_csv(index=False)
                tool_content = f"{len(result_df)} row(s) returned.\n{preview}"
            except qe.QueryError as e:
                tool_content = f"SQL error: {e}"
            contents.append({"role": "user", "parts": [{
                "functionResponse": {"name": fn_call.get("name", "run_sql"),
                                      "response": {"content": tool_content}}
            }]})

        return {"answer": "Ran out of steps trying to answer that — try rephrasing the question.",
                "sql_used": last_sql, "proof_df": last_proof, "error": None}
    except ChatError as e:
        return {"answer": None, "sql_used": last_sql, "proof_df": last_proof, "error": str(e)}
    except requests.RequestException as e:
        return {"answer": None, "sql_used": last_sql, "proof_df": last_proof,
                "error": f"Network error reaching Gemini — check internet: {e}"}


# ==================================================================================
# AUTO-BUILD A KPI CARD OR CHART FROM A PLAIN-LANGUAGE REQUIREMENT
# ==================================================================================
# Separate from the run_sql tool-calling loop above on purpose: this doesn't need
# real numbers back from the data, just a JSON *definition* (which column, which
# measure, which chart type) that app.py then turns into a normal Custom Builder
# KPI card / chart dict — the same shape a person gets from clicking "+ New Card"
# / "+ New Chart" by hand, so it's addable to Raw Analysis / Custom Builder and
# pinnable to the Boss Dashboard exactly the same way.
CARD_CHART_MEASURES = ["Sum", "Average", "Count", "Distinct Count", "Min", "Max", "Median", "Std Dev"]
CARD_CHART_TYPES = ["Bar", "Line", "Pie", "Donut", "Area", "Scatter", "Box", "Histogram", "Treemap", "Heatmap", "Table"]
GRAIN_CODES = ("D", "W", "ME", "YE")

# --------------------------------------------------------------------------------
# NORMALIZING THE AI's ANSWER
# --------------------------------------------------------------------------------
# The free/auto-routed model behind this (openrouter/free — whatever's currently
# available on the free tier) mostly follows the "reply with exactly one of
# these words" instruction above, but not always: it might say "pie chart"
# instead of "Pie", "monthly" instead of "ME", "average" in lowercase, etc.
# app.py used to check these fields with a strict `in` / `==` against the exact
# expected strings, so ANY of those deviations silently fell through to a
# hard-coded default (chart type -> always "Bar", grain -> always None/no
# grouping) with no error shown — which is why chart type looked ignored, and
# why a "monthly trend" request could come out grouped by every individual raw
# timestamp instead of by month (hundreds of tiny slices/bars, and Plotly then
# squashes the actual plot area to fit that huge legend — the "size" looking
# broken was a symptom of this, not a separate bug). Normalize with synonyms
# instead of requiring an exact match.
_CHART_TYPE_ALIASES = {
    "bar": "Bar", "bar chart": "Bar", "column": "Bar", "column chart": "Bar", "bars": "Bar",
    "line": "Line", "line chart": "Line", "trend": "Line", "trendline": "Line",
    "pie": "Pie", "pie chart": "Pie",
    "donut": "Donut", "doughnut": "Donut", "donut chart": "Donut",
    "area": "Area", "area chart": "Area",
    "scatter": "Scatter", "scatter plot": "Scatter", "scatterplot": "Scatter",
    "box": "Box", "box plot": "Box", "boxplot": "Box",
    "histogram": "Histogram", "hist": "Histogram",
    "treemap": "Treemap", "tree map": "Treemap",
    "heatmap": "Heatmap", "heat map": "Heatmap",
    "table": "Table",
}
_MEASURE_ALIASES = {
    "sum": "Sum", "total": "Sum",
    "average": "Average", "avg": "Average", "mean": "Average",
    "count": "Count", "number of": "Count", "no of": "Count",
    "distinct count": "Distinct Count", "unique count": "Distinct Count", "distinct": "Distinct Count", "unique": "Distinct Count",
    "min": "Min", "minimum": "Min",
    "max": "Max", "maximum": "Max",
    "median": "Median",
    "std dev": "Std Dev", "stddev": "Std Dev", "standard deviation": "Std Dev", "std": "Std Dev",
}
_GRAIN_ALIASES = {
    "d": "D", "day": "D", "daily": "D",
    "w": "W", "week": "W", "weekly": "W",
    "me": "ME", "m": "ME", "month": "ME", "monthly": "ME",
    "ye": "YE", "y": "YE", "year": "YE", "yearly": "YE", "annual": "YE", "annually": "YE",
    "none": None, "": None,
}


def _normalize(raw, aliases: dict, valid_values):
    """Case/spacing/synonym-tolerant match against a known set of values.
    Returns the canonical value, or None if nothing reasonable matches."""
    if raw is None:
        return None
    if raw in valid_values:  # already exact — the common case, skip the rest
        return raw
    key = str(raw).strip().lower()
    if key in aliases:
        return aliases[key]
    return None


def normalize_chart_type(raw):
    return _normalize(raw, _CHART_TYPE_ALIASES, CARD_CHART_TYPES)


def normalize_measure(raw):
    return _normalize(raw, _MEASURE_ALIASES, CARD_CHART_MEASURES)


def normalize_grain(raw):
    if raw is None:
        return None
    return _normalize(raw, _GRAIN_ALIASES, GRAIN_CODES)


def _card_chart_system_prompt(df: pd.DataFrame) -> str:
    cols_desc = "\n".join(f"  - {c}: {df[c].dtype}" for c in df.columns)
    return f"""You design ONE KPI card OR ONE chart for a BI dashboard from a plain-language requirement
(Hindi/English mix is fine). Reply with ONLY a single valid JSON object — no prose, no markdown
fences, nothing before or after it.

DATASET COLUMNS (use these EXACT names, case-sensitive — never invent a column that isn't listed)
{cols_desc}

Reply with EXACTLY one of these two JSON shapes:

KPI card (a single headline number):
{{"kind": "kpi", "title": "<short card title>", "column": "<exact column name>",
  "measure": "<one of: {', '.join(CARD_CHART_MEASURES)}>"}}

Chart (a trend, breakdown, comparison, or distribution):
{{"kind": "chart", "title": "<short chart title>",
  "chart_type": "<one of: {', '.join(CARD_CHART_TYPES)}>",
  "x_col": "<exact column name>", "x_grain": "<D, W, ME, YE, or null — only set for a date x_col>",
  "y_col": "<exact column name>", "y_measure": "<one of: {', '.join(CARD_CHART_MEASURES)}>",
  "color_col": "<exact column name, or null>"}}

Use Sum/Average/Median on genuinely numeric columns; use Count/Distinct Count on non-numeric ones.
Pick whichever chart type best fits what was asked for."""


def _call_openrouter_plain(api_key, messages, temperature=0.1):
    """Same endpoint as _call_openrouter but WITHOUT the run_sql tool — used
    for requests that should just return plain JSON, not trigger a SQL call."""
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://sports-analytics-platform.local",
            "X-Title": "Sports Analytics Platform",
        },
        json={"model": OPENROUTER_MODEL, "messages": messages, "temperature": temperature},
        timeout=60,
    )
    if resp.status_code == 401:
        raise ChatError("OpenRouter API key rejected — check the key at openrouter.ai/keys.")
    if resp.status_code == 429:
        raise ChatError("Free-tier rate limit hit — wait a minute and try again.")
    if resp.status_code >= 400:
        raise ChatError(f"OpenRouter API error ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def _call_gemini_plain(api_key, prompt_text, temperature=0.1):
    """Single-turn plain-text Gemini call — no tools, no system_instruction
    split, the whole prompt is just the one user turn (matches how the
    OpenRouter equivalents in this file build their prompt)."""
    data = _call_gemini(api_key, None, [{"role": "user", "parts": [{"text": prompt_text}]}],
                         tools=None, temperature=temperature)
    text, _, _ = _gemini_extract(data)
    return text


# ==================================================================================
# INTELLIGENCE REPORT — NARRATIVE WRITE-UP OVER ALREADY-COMPUTED FACTS
# ==================================================================================
# Unlike the two functions above (`ask`, `suggest_card_or_chart`), this NEVER lets
# the model touch raw data or invent a number — modules/intel_engine.py computes
# every figure in the report with plain pandas first, and this function is only
# ever asked to explain / prioritise / write up numbers it's handed. That split is
# what keeps a free/uncontrolled model from hallucinating a business report.
_REPORT_SECTIONS_EN = (
    "1. Executive Summary (health verdict + why, 3-4 sentences)\n"
    "2. Root-Cause Highlights (for the 2-3 biggest moves in the numbers: Problem -> Evidence -> "
    "Possible Cause -> Business Impact -> Recommended Action. Clearly separate correlation from "
    "confirmed cause.)\n"
    "3. Risks (3-5 risks, each: Risk -> Evidence from the numbers above -> Qualitative confidence "
    "(Low/Medium/High, since no statistical probability was computed) -> Potential Impact -> Mitigation)\n"
    "4. Opportunities (3-5, ranked by likely Impact x Feasibility)\n"
    "5. Top 5 Positive Findings (short bullets, cite the actual numbers)\n"
    "6. Top 5 Problems (short bullets, cite the actual numbers)\n"
    "7. Top 8 Recommended Actions as a markdown table with columns: Priority | Action | Reason | "
    "Expected Impact | Time Horizon (Immediate/30 Days/60-90 Days/3-6 Months)\n"
    "8. Growth Strategy — answer briefly: where to invest more, where to cut, biggest opportunity, "
    "biggest risk, what management should do first\n"
    "9. One-sentence Final Verdict (bold) — the one thing management should remember"
)
_REPORT_SECTIONS_HI = (
    "उपरोक्त सभी सेक्शन हिंदी में लिखें (Hindi/Devanagari script mein), lekin column names, product/customer/"
    "location ke naam aur numbers English/original form mein hi rakhein।"
)


def _report_system_prompt(facts_text: str, language: str) -> str:
    lang_instr = _REPORT_SECTIONS_HI if language == "Hindi" else "Write the whole report in clear, simple English."
    return f"""You are a senior business analyst writing a management report. You are given ONLY
pre-computed, real facts about a dataset below — every number in it was calculated with actual code,
not by you. You must NEVER invent, adjust, or estimate any number yourself — only use the numbers
given. Where a section says "not available", say so plainly instead of guessing. Clearly separate
ACTUAL data from FORECAST data wherever both appear (the facts below label forecasts explicitly).
Never claim causation where only correlation is shown.

FACTS (all real, pre-computed — DATA SOURCE OF TRUTH):
{facts_text}

Write a management report with EXACTLY these sections, in this order, using markdown headers (###):
{_REPORT_SECTIONS_EN}

{lang_instr}
Keep it concise and business-focused — no filler, no repeating the raw facts list verbatim, no
disclaimers about being an AI."""


def generate_report_narrative(facts_text: str, api_key: str, language: str = "English"):
    """Returns {"report": str|None, "error": str|None}. `language` is
    'English', 'Hindi', or 'Both' (caller should call this twice for 'Both',
    once per language, and cache each separately)."""
    if not api_key:
        return {"report": None, "error": "No AI API key configured yet — add one on the AI Assistant page (it's free)."}
    lang = "Hindi" if language == "Hindi" else "English"
    prompt_text = _report_system_prompt(facts_text, lang)
    try:
        if detect_provider(api_key) == "gemini":
            content = _call_gemini_plain(api_key, prompt_text, temperature=0.35)
        else:
            data = _call_openrouter_plain(api_key, [{"role": "user", "content": prompt_text}], temperature=0.35)
            content = data["choices"][0]["message"].get("content", "").strip()
        if not content:
            return {"report": None, "error": "AI returned an empty report — try again."}
        return {"report": content, "error": None}
    except ChatError as e:
        return {"report": None, "error": str(e)}
    except requests.RequestException as e:
        return {"report": None, "error": f"Network error reaching the AI provider — check internet: {e}"}
    except (KeyError, IndexError, TypeError):
        return {"report": None, "error": "AI didn't return a usable report — try again."}


def suggest_card_or_chart(requirement: str, df: pd.DataFrame, api_key):
    """Asks the model to design ONE KPI card or ONE chart matching the loaded
    dataset's real columns. Returns {"spec": dict|None, "error": str|None} —
    `spec` (when present) is a plain dict in one of the two shapes documented
    in _card_chart_system_prompt, not yet a full custom_kpis/custom_charts item."""
    if not api_key:
        return {"spec": None, "error": "No AI API key configured yet — add one on this page (it's free)."}
    system_text = _card_chart_system_prompt(df)
    try:
        if detect_provider(api_key) == "gemini":
            content = _call_gemini_plain(api_key, f"{system_text}\n\n{requirement}")
        else:
            messages = [{"role": "system", "content": system_text}, {"role": "user", "content": requirement}]
            data = _call_openrouter_plain(api_key, messages)
            content = data["choices"][0]["message"].get("content", "").strip()
        content = content.strip("`\n ")
        if content.lower().startswith("json"):
            content = content[4:].strip()
        spec = json.loads(content)
        if spec.get("kind") not in ("kpi", "chart"):
            return {"spec": None, "error": "AI didn't return a recognisable card/chart definition — try rephrasing."}
        return {"spec": spec, "error": None}
    except ChatError as e:
        return {"spec": None, "error": str(e)}
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return {"spec": None, "error": "AI didn't return a valid card/chart definition — try rephrasing your requirement."}
    except requests.RequestException as e:
        return {"spec": None, "error": f"Network error reaching OpenRouter — check internet: {e}"}
