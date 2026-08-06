# 📊 REPORT_GEN_AI

A multi-tenant, no-code BI (Business Intelligence) dashboard builder — built with **Python + Streamlit**.
Upload any dataset and instantly get auto-generated KPIs, charts, pivot tables, and an AI assistant
that answers questions about your data in plain English (backed by real SQL, not guesses).

> Built as a full end-to-end product: auth & roles, drag-and-drop dashboard builder, natural-language
> SQL querying, Excel-style pivots, PDF report export, and an AI data-chat assistant.

---

## ✨ Features

### 📊 Auto-generated dashboards
Upload a CSV/Excel file and the app automatically profiles every column (numeric, categorical,
date, ID-like) and generates sensible KPI cards and chart suggestions — no manual field mapping
needed to get started.

### 🧩 Custom Dashboard Builder ("Boss Dashboard")
- Drag-and-drop-style builder to create your own KPI cards and charts on top of any column/measure.
- Every card/chart can have its **own independent filters**, separate from the page-level filter.
- A **global format & measure toolbar** to restyle every card at once (number format: Auto / Full /
  Compact, currency, etc).
- Pin any auto-generated KPI or chart straight to the dashboard.

### 🧮 Excel-style Pivot Reports
Build multi-level row/column pivot tables (like Excel PivotTables) on top of a DuckDB query engine —
stays fast even on very large datasets, since the heavy GROUP BY work happens inside DuckDB and only
the small aggregated result gets reshaped into a pivot.

### 🤖 AI Assistant — chat with your data
Ask questions in plain language ("what's the trend over the last 3 months?", "which record is
number 5?"). The assistant doesn't guess — it writes a real, read-only SQL query, runs it against
your dataset through a sandboxed DuckDB engine, and grounds its answer in the actual result (shown
as "proof" under the chat reply). Powered by a free OpenRouter model with tool-calling.

### 🗄️ Bring-your-own database
Beyond file uploads, connect any SQL database SQLAlchemy supports (PostgreSQL, MySQL, SQL Server,
Oracle, SQLite...) and query it directly. Only read-only `SELECT`/`WITH` statements are allowed —
credentials are kept in-memory for the session only and are never written to disk.

### 📄 One-click PDF export
Turn any dashboard into a clean, print-ready PDF report — branded masthead, styled KPI cards, and
chart pages with an auto-generated "Insight" callout, ready to send to a client.

### 🔐 Multi-tenant, role-based access
Three roles, each with its own permissions:
- **Admin** — manages all accounts, resets passwords, can inspect any workspace, but doesn't own
  data itself.
- **Client** — has their own private, independent data workspace (upload, build, export).
- **Viewer** — read-only access to a specific client's dashboards, for sharing with a client's team
  without letting them edit anything.

Every workspace's data is completely isolated from every other workspace.

---

## 🛠️ Tech Stack

| Layer               | Technology                              |
|----------------------|------------------------------------------|
| UI / App framework   | [Streamlit](https://streamlit.io)        |
| Data processing       | pandas                                   |
| Query engine (SQL)    | DuckDB (sandboxed, read-only)            |
| Any-database connector| SQLAlchemy                               |
| Charts                | Plotly                                   |
| PDF generation         | ReportLab                                |
| AI Assistant           | OpenRouter (OpenAI-compatible, tool-calling) |
| Auth / persistence      | Custom role-based auth + pickle-based workspace storage |

---

## 📂 Project Structure

```
sports_app/
├── app.py                     # Main Streamlit app — all pages & routing
├── requirements.txt
├── requirements-db-connector.txt
└── modules/
    ├── auth.py                # Role-based login (admin / client / viewer)
    ├── data_engine.py         # Column profiling + auto KPI generation
    ├── chart_engine.py        # Auto chart-variant generation
    ├── builder_engine.py      # Custom dashboard builder (per-card filters, formatting)
    ├── pivot_engine.py        # Excel-style pivot report engine (on DuckDB)
    ├── query_engine.py        # Sandboxed, read-only SQL engine (DuckDB)
    ├── db_connector.py        # Bring-your-own-database connector (SQLAlchemy)
    ├── ai_chat.py              # Natural-language "Chat with your Data" assistant
    ├── pdf_export.py           # Dashboard → PDF report export
    ├── measures.py             # Aggregation / number-formatting helpers
    └── workspace_store.py      # Per-tenant workspace persistence
```

---

## 🚀 Running Locally

```bash
git clone <this-repo-url>
cd sports_app
pip install -r requirements.txt
streamlit run app.py
```

To enable the AI Assistant, get a free key from [openrouter.ai](https://openrouter.ai) and either:

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

or create `.streamlit/secrets.toml`:

```toml
OPENROUTER_API_KEY = "sk-or-v1-..."
```

---

## 📌 Note on this repo

This is a **portfolio / demo version** of a client project — real client data, login credentials,
and saved workspaces have been removed. The live, deployed version of this app is used privately by
a client with their own data and accounts.

---

## 👤 Author

Built by **Ankit Solanki** — Tool Developer & Creator.
