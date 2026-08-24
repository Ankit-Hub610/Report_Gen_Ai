"""
ai_chat.py
----------
Free, natural-language "Chat with your Data" assistant.

Uses OpenRouter (https://openrouter.ai) which has a genuinely free tier —
no credit card required — and an OpenAI-compatible endpoint, so this talks
to it with plain `requests`, no extra SDK needed.

NOTE (why OpenRouter and not Groq): Groq's free API blocks requests coming
from datacenter/cloud-hosted IPs (its own anti-abuse policy) — so it works
when you run the app on your own laptop, but fails with a 403
"Access denied. Please check your network settings." the moment the app is
deployed to Streamlit Community Cloud, Render, or any other cloud host.
OpenRouter does not have that restriction, so this works the same whether
you run locally or deployed.

We use the model id "openrouter/free" — OpenRouter's own auto-router that
picks a currently-available free model that supports tool calling. Free
model line-ups on OpenRouter rotate every few weeks, so pinning one exact
free model name tends to break later; the auto-router avoids that.

HOW IT ANSWERS ACCURATELY (not just guessing from memory):
The model is given a "run_sql" tool. For anything that needs real numbers
(a specific record, a trend over the last N months, a comparison, etc.)
the model writes a read-only SQL query, we run it through the SAME sandboxed
DuckDB engine already used by the SQL Query tab (modules/query_engine.py —
read-only, no writes, no file access), and feed the actual result back to
the model so its final answer is grounded in the real data instead of
hallucinated. The SQL + result table are also returned so the UI can show
them as "proof" under the chat answer.

SETUP (one-time, free):
  1. Go to https://openrouter.ai -> sign up (free, no card) -> Keys -> Create key.
  2. Either:
       a) set environment variable OPENROUTER_API_KEY before running streamlit, OR
       b) create .streamlit/secrets.toml with:  OPENROUTER_API_KEY = "sk-or-v1-..."
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
MAX_TOOL_ROUNDS = 3
MAX_ROWS_TO_MODEL = 40   # cap how many result rows get sent back into the prompt (keeps cost/latency low)


class ChatError(Exception):
    pass


def get_api_key():
    """Looks for a key the admin already configured (env var or Streamlit
    secrets) before falling back to whatever the admin typed into the UI
    this session."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    try:
        import streamlit as st
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
    return "\n".join(f"  - {k['label']}: {k['value']} ({k.get('sub','')})" for k in kpis)


def _dashboard_summary(dashboard_charts: list) -> str:
    if not dashboard_charts:
        return "(no charts pinned to the Boss Dashboard yet)"
    out = []
    for c in dashboard_charts:
        v = c["variant"]
        out.append(f"  - [{c['family']}] {v.get('title', v.get('id'))}")
    return "\n".join(out)


def _system_prompt(df, meta, kpis, dashboard_charts):
    return f"""You are a helpful AI assistant embedded in a BI dashboard app. You can do TWO kinds
of things, and you should figure out which one a question needs:

1. QUESTIONS ABOUT THE USER'S OWN LOADED DATASET (their business data, KPIs, charts) — for
   these, you MUST ground every number in a real run_sql result. Never guess or make up numbers
   for the user's data.
2. ANYTHING ELSE — general knowledge, explanations, advice, definitions, how-to help, casual
   conversation, sports/news/trivia, writing help, math, coding help, translations, etc. — answer
   these directly and helpfully from your own knowledge, exactly like a normal general-purpose AI
   assistant (e.g. ChatGPT) would. Do NOT refuse or deflect a question just because it isn't about
   the loaded dataset — only the DATASET-related numbers need the run_sql grounding rule above;
   your general knowledge answers don't need a tool call at all.

Reply in clear, simple language (Hindi/English mix is fine if the user writes that way).

DATASET
{_dataset_summary(df, meta)}

CURRENT KPI CARDS ON THE DASHBOARD
{_kpi_summary(kpis)}

CHARTS ALREADY PINNED TO THE BOSS DASHBOARD
{_dashboard_summary(dashboard_charts)}

TOOL AVAILABLE: run_sql(query)
- Runs a single read-only SQL SELECT (DuckDB syntax) against the table `{qe.DEFAULT_TABLE_NAME}` and
  returns the result rows.
- Use it ONLY when the question is actually about the user's loaded dataset and needs an actual
  number, record, trend, or comparison from it —
  e.g. "which record is number 5" -> SELECT * FROM {qe.DEFAULT_TABLE_NAME} LIMIT 1 OFFSET 4;
  "last 3 months trend" -> GROUP BY month on the date column, filtered to the last 3 months.
- For anything NOT about this dataset, don't call this tool at all — just answer normally.
- Only SELECT/WITH is allowed. No INSERT/UPDATE/DELETE/DDL.
- You may call it more than once if the first query needs refining.

WHEN YOU ANSWER A DATASET QUESTION
- Ground every number in a run_sql result — don't estimate.
- Explain the finding in plain language first, then the supporting number(s).
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
       {answer: str, sql_used: str|None, proof_df: DataFrame|None, error: str|None}"""
    if not api_key:
        return {"answer": None, "sql_used": None, "proof_df": None,
                "error": "No OpenRouter API key configured yet — add one below (it's free)."}

    messages = [{"role": "system", "content": _system_prompt(df, meta, kpis, dashboard_charts)}]
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
        return {"report": None, "error": "No OpenRouter API key configured yet — add one on the AI Assistant page (it's free)."}
    lang = "Hindi" if language == "Hindi" else "English"
    messages = [{"role": "user", "content": _report_system_prompt(facts_text, lang)}]
    try:
        data = _call_openrouter_plain(api_key, messages, temperature=0.35)
        content = data["choices"][0]["message"].get("content", "").strip()
        if not content:
            return {"report": None, "error": "AI returned an empty report — try again."}
        return {"report": content, "error": None}
    except ChatError as e:
        return {"report": None, "error": str(e)}
    except requests.RequestException as e:
        return {"report": None, "error": f"Network error reaching OpenRouter — check internet: {e}"}
    except (KeyError, IndexError, TypeError):
        return {"report": None, "error": "AI didn't return a usable report — try again."}


def suggest_card_or_chart(requirement: str, df: pd.DataFrame, api_key):
    """Asks the model to design ONE KPI card or ONE chart matching the loaded
    dataset's real columns. Returns {"spec": dict|None, "error": str|None} —
    `spec` (when present) is a plain dict in one of the two shapes documented
    in _card_chart_system_prompt, not yet a full custom_kpis/custom_charts item."""
    if not api_key:
        return {"spec": None, "error": "No OpenRouter API key configured yet — add one on this page (it's free)."}
    messages = [
        {"role": "system", "content": _card_chart_system_prompt(df)},
        {"role": "user", "content": requirement},
    ]
    try:
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
