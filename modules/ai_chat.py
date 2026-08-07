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
    return f"""You are a data analyst assistant embedded in a BI dashboard. You answer the user's
questions about THEIR loaded dataset in clear, simple language (Hindi/English mix is fine if the
user writes that way), always backed by real numbers — never guess or make up numbers.

DATASET
{_dataset_summary(df, meta)}

CURRENT KPI CARDS ON THE DASHBOARD
{_kpi_summary(kpis)}

CHARTS ALREADY PINNED TO THE BOSS DASHBOARD
{_dashboard_summary(dashboard_charts)}

TOOL AVAILABLE: run_sql(query)
- Runs a single read-only SQL SELECT (DuckDB syntax) against the table `{qe.DEFAULT_TABLE_NAME}` and
  returns the result rows.
- Use it whenever the answer needs an actual number, record, trend, or comparison from the data —
  e.g. "which record is number 5" -> SELECT * FROM {qe.DEFAULT_TABLE_NAME} LIMIT 1 OFFSET 4;
  "last 3 months trend" -> GROUP BY month on the date column, filtered to the last 3 months.
- Only SELECT/WITH is allowed. No INSERT/UPDATE/DELETE/DDL.
- You may call it more than once if the first query needs refining.

WHEN YOU ANSWER
- Ground every number in a run_sql result — don't estimate.
- Explain the finding in plain language first, then the supporting number(s).
- When it's useful, suggest which chart type would visualise this well and which columns to put
  on which axis (e.g. "a Line chart with {meta.get('primary_date','the date column')} on X and
  the totals on Y would show this trend clearly").
- Keep answers concise — a short paragraph or a few bullet points, not an essay.
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
