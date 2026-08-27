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
GEMINI_MODEL_DEFAULT = "gemini-3.6-flash"   # fixed model — see module docstring for why that matters.
# Google occasionally retires/renames free Gemini models (e.g. gemini-2.0-flash was retired in
# 2026). Rather than hardcoding one name that breaks again later, an admin can override it without
# touching code: set GEMINI_MODEL as an env var or Streamlit secret to whatever model name Google's
# error message (or https://ai.google.dev/gemini-api/docs/models) currently recommends.
MAX_TOOL_ROUNDS = 3
MAX_ROWS_TO_MODEL = 40   # cap how many result rows get sent back into the prompt (keeps cost/latency low)


GEMINI_IMAGE_MODEL_DEFAULT = "gemini-2.5-flash-image"   # "Nano Banana" — Google's free-tier
# image-generation model, same API key as the text model above. Same override mechanism as
# GEMINI_MODEL, via GEMINI_IMAGE_MODEL, in case Google renames/retires this one too later.


def get_gemini_model():
    """Resolves the Gemini model name at call time (not import time) so an
    admin-set override in secrets/env always wins over the built-in default."""
    model = os.environ.get("GEMINI_MODEL")
    if model:
        return model
    try:
        import streamlit as st
        if "GEMINI_MODEL" in st.secrets:
            return st.secrets["GEMINI_MODEL"]
    except Exception:
        pass
    return GEMINI_MODEL_DEFAULT


def get_gemini_image_model():
    model = os.environ.get("GEMINI_IMAGE_MODEL")
    if model:
        return model
    try:
        import streamlit as st
        if "GEMINI_IMAGE_MODEL" in st.secrets:
            return st.secrets["GEMINI_IMAGE_MODEL"]
    except Exception:
        pass
    return GEMINI_IMAGE_MODEL_DEFAULT


def _gemini_url():
    return f"https://generativelanguage.googleapis.com/v1beta/models/{get_gemini_model()}:generateContent"


def _gemini_image_url():
    return f"https://generativelanguage.googleapis.com/v1beta/models/{get_gemini_image_model()}:generateContent"


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
        return """You are a helpful, all-in-one, general-purpose AI assistant embedded in a BI dashboard app —
like ChatGPT, the user can type anything into one box and you figure out what they meant, with no
manual mode-picker. No dataset is loaded in this session right now, so:

- If the user is asking you to DRAW/DESIGN/GENERATE AN IMAGE (banner, logo, poster, illustration,
  wallpaper, icon, artwork, etc.), call the generate_image tool if it's offered to you below. If it
  isn't offered (e.g. a view-only account), say so plainly instead of pretending.
- If the user asks something that clearly needs THEIR OWN business data/chart-building (e.g. "what
  was my revenue last month", "ek chart banao"), gently let them know you don't see any dataset
  loaded yet and they can load one on the 📥 Connect Data page — then answer whatever you still
  reasonably can in general terms.
- For everything else — general knowledge, explanations, advice, definitions, how-to help, casual
  conversation, sports/news/trivia, writing help, math, coding help, translations, etc. — just
  answer directly and helpfully from your own knowledge, exactly like a normal general-purpose AI
  assistant would.

Reply in clear, simple language (Hindi/English mix is fine if the user writes that way). Keep
answers concise — a short paragraph or a few bullet points, not an essay."""

    return f"""You are a helpful, all-in-one AI assistant embedded in a BI dashboard app — like ChatGPT, the
user should be able to type ANYTHING into one box and have you figure out what they meant, with no
manual mode-picker. You can do FOUR kinds of things; decide which one a message needs from its
wording alone, exactly once, without asking the user to clarify which mode they meant:

1. QUESTIONS ABOUT THE USER'S OWN LOADED DATASET (their business data, KPIs, charts, trend history,
   or forecast) — ground every number either in the PRE-COMPUTED PAST TREND / FUTURE FORECAST facts
   given to you below, or, for anything not already covered there, in a real run_sql result. Never
   guess or make up numbers for the user's data.
2. A request to DRAW/DESIGN/GENERATE AN IMAGE (a banner, logo, poster, illustration, wallpaper,
   icon, artwork, etc.) — call the generate_image tool if it's offered to you below. If it isn't
   offered (view-only account, or no image tool available), say so plainly instead of pretending.
3. A request to BUILD A NEW CHART OR KPI CARD for their dashboard from their own data (e.g.
   "monthly revenue trend banao", "top clients ka chart banao") — call design_card_or_chart if it's
   offered to you below.
4. ANYTHING ELSE — general knowledge, explanations, advice, definitions, how-to help, casual
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


# --------------------------------------------------------------------------------
# ONE UNIFIED INPUT, THREE POSSIBLE INTENTS — CHATGPT-STYLE AUTO-DETECTION
# --------------------------------------------------------------------------------
# BUG FIX (reported): the AI Assistant page used to make the user manually pick
# from 3 mode buttons ("💬 Poocho" / "🪄 Card/Chart banao" / "🖼️ Image banao")
# before typing, which looked unprofessional next to a normal chat box - "usko
# kuch bhi bolo vo samajh jata he" (ChatGPT-like: tell it anything, it figures
# out what you meant) was the ask. Fixed by giving the model itself THREE tools
# in every call instead of three separate hand-picked code paths - it decides,
# from the wording alone, whether a message needs `run_sql` (a data question),
# `generate_image` (draw/design something), or `design_card_or_chart` (build a
# new dashboard chart/KPI from their data), or none of those (just answer in
# plain text). app.py now has exactly ONE input box; see ask()'s "kind" field
# in its return value for how the caller renders whichever one the model chose.
_RUN_SQL_DECL = {
    "name": "run_sql",
    "description": "Run a read-only SQL SELECT query against the loaded dataset and return the results.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A single read-only SQL SELECT/WITH statement."}
        },
        "required": ["query"],
    },
}
_GENERATE_IMAGE_DECL = {
    "name": "generate_image",
    "description": (
        "Generate a picture (banner, logo, illustration, poster, icon, wallpaper, artwork, etc.) from a "
        "plain-language description. Call this whenever the user is asking you to draw/design/generate/create "
        "an IMAGE or VISUAL ARTWORK - NOT for a chart/graph of their data, use design_card_or_chart for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string",
                       "description": "A clear, detailed image-generation prompt capturing exactly what the user asked for, in English."}
        },
        "required": ["prompt"],
    },
}
_DESIGN_CARD_CHART_DECL = {
    "name": "design_card_or_chart",
    "description": (
        "Design ONE new KPI card or ONE new chart for the dashboard, built from the user's OWN loaded dataset. "
        "Call this whenever the user asks you to build/add/create a chart, graph, KPI card, or metric for their "
        "dashboard (e.g. 'monthly revenue trend banao', 'top clients by total paid ka chart banao') - NOT for a "
        "generic image/artwork request, use generate_image for those."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "requirement": {"type": "string",
                             "description": "The user's requirement in their own words, e.g. 'monthly revenue trend' or 'top 5 clients by total paid'."}
        },
        "required": ["requirement"],
    },
}


def _build_openai_tools(has_data: bool, can_edit: bool):
    """Only offers each tool when it can actually be fulfilled: run_sql needs
    a loaded dataset; design_card_or_chart needs BOTH a dataset and edit
    rights (view-only accounts can chat but not build); generate_image only
    needs edit rights. Returns None (no tools at all) if nothing qualifies -
    a plain-text-only call, same as before this feature existed."""
    decls = []
    if has_data:
        decls.append(_RUN_SQL_DECL)
    if can_edit:
        decls.append(_GENERATE_IMAGE_DECL)
        if has_data:
            decls.append(_DESIGN_CARD_CHART_DECL)
    if not decls:
        return None
    return [{"type": "function", "function": d} for d in decls]


def _build_gemini_tools(has_data: bool, can_edit: bool):
    decls = []
    if has_data:
        decls.append(_RUN_SQL_DECL)
    if can_edit:
        decls.append(_GENERATE_IMAGE_DECL)
        if has_data:
            decls.append(_DESIGN_CARD_CHART_DECL)
    if not decls:
        return None
    return [{"function_declarations": decls}]


def _result(kind="text", answer=None, sql_used=None, proof_df=None,
            image_b64=None, mime_type=None, spec=None, error=None):
    """Canonical return shape for ask() regardless of which of the 3 intents
    the model picked. `kind` tells app.py how to render this turn:
      "text"       -> normal chat bubble (`answer`, optionally `sql_used`/`proof_df` as proof)
      "image"      -> an image bubble (`image_b64` + `mime_type`, `answer` as an optional caption)
      "card_chart" -> the existing Add/Pin-to-dashboard preview UI, built from `spec`
    `error` set means something went wrong — callers should show it and not
    trust the other fields."""
    return {"kind": kind, "answer": answer, "sql_used": sql_used, "proof_df": proof_df,
            "image_b64": image_b64, "mime_type": mime_type, "spec": spec, "error": error}


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
        _gemini_url(),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        json=body,
        timeout=60,
    )
    if resp.status_code in (401, 403):
        raise ChatError("Gemini API key rejected — check the key at aistudio.google.com "
                         "(Get API key). Make sure it was pasted in full, with no extra spaces.")
    if resp.status_code == 404:
        raise ChatError(f"Gemini model '{get_gemini_model()}' is no longer available (Google "
                         f"retires old free models over time). Set GEMINI_MODEL in Secrets to "
                         f"whatever model name Google's error/docs currently recommend, then "
                         f"reboot the app. Raw error: {resp.text[:200]}")
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


def _call_openrouter(api_key, messages, tools=None):
    body = {"model": OPENROUTER_MODEL, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    resp = requests.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            # OpenRouter-recommended headers (used for free-tier rankings, not required to work)
            "HTTP-Referer": "https://sports-analytics-platform.local",
            "X-Title": "Sports Analytics Platform",
        },
        json=body,
        timeout=60,
    )
    if resp.status_code == 401:
        raise ChatError("OpenRouter API key rejected — check the key at openrouter.ai/keys.")
    if resp.status_code == 429:
        raise ChatError("Free-tier rate limit hit — wait a minute and try again.")
    if resp.status_code >= 400:
        raise ChatError(f"OpenRouter API error ({resp.status_code}): {resp.text[:300]}")
    return resp.json()


def ask(question, df, meta, kpis, dashboard_charts, api_key, history=None, can_edit=False):
    """Runs the tool-calling loop and lets the model itself pick the intent —
    a data/general question, an image request, or a new-chart/KPI-card
    request (see the 3 tool declarations above) - no manual mode picker
    needed on the caller's side. Returns a dict shaped by _result() above;
    check `kind` to know how to render it.

    `can_edit` gates whether the image/card-chart tools are even offered -
    pass False (the default) for any read-only/follow-up Q&A surface that
    should only ever produce plain-text answers (e.g. the Full Analysis
    page's follow-up box); the main 🤖 AI Assistant page passes the real
    can_edit() so only editors can trigger those two.

    Routes to Gemini or OpenRouter based on the key's shape (see
    detect_provider) — this is the one place that decides, everything
    downstream is provider-specific."""
    if not api_key:
        return _result(error="No AI API key configured yet — add one below (it's free).")

    system_prompt = _system_prompt(df, meta, kpis, dashboard_charts)
    if detect_provider(api_key) == "gemini":
        return _ask_gemini(question, df, system_prompt, api_key, history, can_edit)
    return _ask_openrouter(question, df, system_prompt, api_key, history, can_edit)


def _ask_openrouter(question, df, system_prompt, api_key, history, can_edit):
    tools = _build_openai_tools(has_data=df is not None, can_edit=can_edit)
    messages = [{"role": "system", "content": system_prompt}]
    for turn in (history or [])[-6:]:   # keep a little chat memory, capped to avoid huge prompts
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    last_sql, last_proof = None, None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            data = _call_openrouter(api_key, messages, tools=tools)
            choice = data["choices"][0]["message"]
            tool_calls = choice.get("tool_calls")

            if not tool_calls:
                return _result(answer=choice.get("content", "").strip(), sql_used=last_sql, proof_df=last_proof)

            # An image or card/chart request ends the turn right here — only
            # run_sql calls loop back for further refinement.
            for tc in tool_calls:
                name = tc["function"]["name"]
                if name in ("generate_image", "design_card_or_chart"):
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    if name == "generate_image":
                        img = generate_image(args.get("prompt", question), api_key)
                        return _result(kind="image", answer=img.get("text"), image_b64=img.get("image_b64"),
                                       mime_type=img.get("mime_type"), error=img.get("error"))
                    cc = suggest_card_or_chart(args.get("requirement", question), df, api_key)
                    return _result(kind="card_chart", spec=cc.get("spec"), error=cc.get("error"))

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

        return _result(answer="Ran out of steps trying to answer that — try rephrasing the question.",
                        sql_used=last_sql, proof_df=last_proof)
    except ChatError as e:
        return _result(sql_used=last_sql, proof_df=last_proof, error=str(e))
    except requests.RequestException as e:
        return _result(sql_used=last_sql, proof_df=last_proof,
                        error=f"Network error reaching OpenRouter — check internet: {e}")


def _ask_gemini(question, df, system_prompt, api_key, history, can_edit):
    contents = []
    for turn in (history or [])[-6:]:
        role = "model" if turn["role"] == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": turn["content"]}]})
    contents.append({"role": "user", "parts": [{"text": question}]})

    tools = _build_gemini_tools(has_data=df is not None, can_edit=can_edit)
    last_sql, last_proof = None, None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            data = _call_gemini(api_key, system_prompt, contents, tools=tools, temperature=0.2)
            text, fn_call, model_content = _gemini_extract(data)

            if not fn_call:
                return _result(answer=text, sql_used=last_sql, proof_df=last_proof)

            name = fn_call.get("name")
            args = fn_call.get("args") or {}

            if name == "generate_image":
                img = generate_image(args.get("prompt", question), api_key)
                return _result(kind="image", answer=img.get("text"), image_b64=img.get("image_b64"),
                               mime_type=img.get("mime_type"), error=img.get("error"))
            if name == "design_card_or_chart":
                cc = suggest_card_or_chart(args.get("requirement", question), df, api_key)
                return _result(kind="card_chart", spec=cc.get("spec"), error=cc.get("error"))

            contents.append(model_content)   # the model's turn, including its functionCall part
            sql = args.get("query", "")
            try:
                result_df = qe.run_sql(df, sql)
                last_sql, last_proof = sql, result_df
                preview = result_df.head(MAX_ROWS_TO_MODEL).to_csv(index=False)
                tool_content = f"{len(result_df)} row(s) returned.\n{preview}"
            except qe.QueryError as e:
                tool_content = f"SQL error: {e}"
            contents.append({"role": "user", "parts": [{
                "functionResponse": {"name": name or "run_sql", "response": {"content": tool_content}}
            }]})

        return _result(answer="Ran out of steps trying to answer that — try rephrasing the question.",
                        sql_used=last_sql, proof_df=last_proof)
    except ChatError as e:
        return _result(sql_used=last_sql, proof_df=last_proof, error=str(e))
    except requests.RequestException as e:
        return _result(sql_used=last_sql, proof_df=last_proof,
                        error=f"Network error reaching Gemini — check internet: {e}")


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

# ==================================================================================
# IMAGE GENERATION (Gemini "Nano Banana" — free tier, same API key as the text model)
# ==================================================================================
# Deliberately Gemini-only: OpenRouter's free/auto-routed tier doesn't reliably expose
# an image-output model, so asking it would silently fail or pick something odd. If the
# admin's configured key is an OpenRouter key, this tells the user plainly to add a
# Gemini key instead, rather than pretending to generate something it can't.

def generate_image(prompt: str, api_key: str):
    """Returns {"image_b64": str|None, "mime_type": str|None, "text": str|None,
    "error": str|None}. image_b64 is raw base64 (no data: prefix) — caller decides
    how to display/store it."""
    if not api_key:
        return {"image_b64": None, "mime_type": None, "text": None,
                "error": "No AI API key configured yet — add one on the AI Assistant page (it's free)."}
    if detect_provider(api_key) != "gemini":
        return {"image_b64": None, "mime_type": None, "text": None,
                "error": "Image generation needs a Gemini API key (OpenRouter's free tier doesn't "
                         "reliably support it). Ask your admin to add a Gemini key from "
                         "aistudio.google.com in Secrets as GEMINI_API_KEY."}

    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    try:
        resp = requests.post(
            _gemini_image_url(),
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json=body,
            timeout=90,
        )
        if resp.status_code in (401, 403):
            return {"image_b64": None, "mime_type": None, "text": None,
                    "error": "Gemini API key rejected — check the key at aistudio.google.com."}
        if resp.status_code == 404:
            return {"image_b64": None, "mime_type": None, "text": None,
                    "error": f"Gemini image model '{get_gemini_image_model()}' is no longer available. "
                             f"Set GEMINI_IMAGE_MODEL in Secrets to whatever model name Google's docs "
                             f"currently recommend for image generation, then reboot the app."}
        if resp.status_code == 429:
            return {"image_b64": None, "mime_type": None, "text": None,
                    "error": "Free-tier image quota hit for now — wait a bit and try again."}
        if resp.status_code >= 400:
            return {"image_b64": None, "mime_type": None, "text": None,
                    "error": f"Gemini image API error ({resp.status_code}): {resp.text[:300]}"}

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            block_reason = (data.get("promptFeedback") or {}).get("blockReason")
            reason_txt = f" (reason: {block_reason})" if block_reason else ""
            return {"image_b64": None, "mime_type": None, "text": None,
                    "error": f"Gemini declined to generate that image{reason_txt}. Try rephrasing."}

        parts = candidates[0].get("content", {}).get("parts", [])
        image_b64, mime_type, text = None, None, None
        for p in parts:
            if "inlineData" in p:
                image_b64 = p["inlineData"].get("data")
                mime_type = p["inlineData"].get("mimeType", "image/png")
            elif "text" in p:
                text = (text or "") + p["text"]

        if not image_b64:
            return {"image_b64": None, "mime_type": None, "text": text,
                    "error": "Gemini didn't return an image for that — try rephrasing the request."}
        return {"image_b64": image_b64, "mime_type": mime_type, "text": text, "error": None}
    except requests.RequestException as e:
        return {"image_b64": None, "mime_type": None, "text": None,
                "error": f"Network error reaching Gemini — check internet: {e}"}
