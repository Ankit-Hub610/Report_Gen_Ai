![RA-I — Research | Analysis | Intelligence](assets/ra-i-logo.png)

### Turn any spreadsheet into a boardroom-ready dashboard — no formulas, no analyst required.

**Built with:** Python · Streamlit · Pandas · Plotly

---

## 📸 Screenshots

<img width="888" height="563" alt="image" src="https://github.com/user-attachments/assets/aa108db7-fe9a-4d56-90f1-391e95efde1b" />
<img width="1913" height="697" alt="image" src="https://github.com/user-attachments/assets/bb6d7e3a-3700-4f90-8dfb-2a14a141b22e" />
<img width="1565" height="706" alt="image" src="https://github.com/user-attachments/assets/f7073bcf-81b5-47c7-bb31-12a036f6fea3" />
<img width="1561" height="828" alt="image" src="https://github.com/user-attachments/assets/d98f1a46-2e0a-4908-8431-8da3e610a6d8" />
<img width="1577" height="752" alt="image" src="https://github.com/user-attachments/assets/d5af4d4f-12b7-4f85-9f02-172d94400317" />
<img width="1606" height="580" alt="image" src="https://github.com/user-attachments/assets/5c4e75d1-443b-4226-a3e1-bafeef94414c" />
<img width="1702" height="887" alt="image" src="https://github.com/user-attachments/assets/fad36e29-447a-4959-87e9-f84211089182" />


## 👉 Try it live

**App:** [reportgenai-2uk4jqmjulsachx5vgg6wz.streamlit.app](https://reportgenai-2uk4jqmjulsachx5vgg6wz.streamlit.app/?stoken=krfZ0Z2dV6YxOq6AcYVYdtcjg_sk1IaQ5UMo3HonLvI&s=2AZbDAbhuUdheRQFnKshpanFG3DBoYnE)

| Role | Username | Password |
|---|---|---|
| 🔍 Demo (read & explore) | `demo` | `demo123` |

> 🔐 An **admin** role also exists behind the scenes — it manages client accounts, API keys, and platform-wide settings. Admin credentials are kept private and aren't part of the public demo; the `demo` login above is the intended way to explore the product.

---

## 🎯 Objective — why this exists

Most small businesses, sports academies, coaching centers, and payment-link sellers sit on **raw transaction/booking data** (a CSV or Excel export) but have **no analyst, no BI team, and no time to build a dashboard by hand**.

This platform closes that gap end-to-end:

- 📥 **Upload any file** (CSV, XLSX, JSON, or a table-based PDF) — no fixed schema, no template to follow.
- 🧩 **Columns are auto-detected**, not hard-coded — the same tool works whether the data is about sports bookings, sales, HR, or finance.
- 📊 **KPIs, charts, and a full narrative report are generated automatically**, with every number computed by real code (pandas/NumPy) — the AI layer only ever *explains and prioritizes* numbers that were already calculated, it never invents one.
- ⭐ **A client-facing "Boss Dashboard"** lets you curate exactly what to show, style it to match your brand, and export a clean, presentation-ready PDF in one click.
- 🔐 **Multi-tenant from day one** — every client's data lives in its own isolated workspace, with role-based access (Admin / Client / Report Viewer) so a client can safely hand a read-only link to their own boss or manager.

In short: **it's the analyst-in-a-box for anyone who has data but not a data team.**

---

## 🧭 What's inside

| Page | What it does |
|---|---|
| 📥 **Connect Data** | Upload a file or connect a live database — auto-cleans and profiles every column |
| 📊 **Raw Analysis** | Instant KPI cards + 10 auto-generated chart types (Bar, Line, Pie, Scatter, Heatmap...), each with a one-line auto-insight |
| 🧩 **Custom Builder** | Power BI–style: build your own KPI cards and charts with per-card filters |
| ⭐ **Boss Dashboard** | Curated, presentation-ready view — full theme control, chart zoom, and one-click PDF export that matches exactly what's on screen |
| 💡 **Business Insights** | Ready-made breakdown for payment-link / booking-style datasets |
| 📈 **Full Analysis** | The deep dive: data-quality notes, trend & forecast, anomalies, correlations, top/bottom breakdowns, and an AI-written plain-language summary — past performance, what's likely next, and where the business is winning or losing money |
| 🤖 **AI Assistant** | Ask questions about your data in plain English/Hindi — every answer is backed by a real query, never guessed |
| 🗂 **Data Table** | SQL-style filter, sort, and export of the raw data |
| ⚙️ **Settings / 🔐 Admin Panel** | Branding, theming, report-viewer accounts, and platform-wide controls |

---

## 👥 Who is this for

- 🏸 **Sports academies, coaching centers & booking platforms** (pickleball, badminton, turf, gyms) — turn payment-link exports into a management report in minutes
- 🛍️ **Small e-commerce / D2C sellers** — revenue, repeat-customer rate, and product performance without a BI hire
- 💳 **Payment-link / booking-tool businesses** — the built-in "Business Insights" page is purpose-built for this data shape
- 🏢 **Agencies managing multiple clients** — multi-tenant workspaces mean one deployment can serve many clients, each fully isolated
- 📈 **Founders & small business owners** — anyone who wants "what happened, why, and what to do next" without hiring an analyst
- 🧑‍💼 **Freelance consultants / analysts** — white-label this as a fast, branded reporting tool for your own clients

---

## 🛠️ Tech Stack

`Python` · `Streamlit` · `Pandas` / `NumPy` · `Plotly` · `ReportLab` (PDF export) · `SQLAlchemy` (optional database connections) · An LLM (via OpenRouter) for the plain-language narrative layer

---

## ✨ Highlights

- Works on **any** tabular dataset — nothing about column names is hard-coded
- Every number is **code-calculated first, explained by AI second** — no hallucinated figures
- **Multi-tenant, role-based access** (Admin / Client / Report Viewer) built in from the ground up
- **Chart zoom syncs to the exported PDF** — what you see on screen is exactly what your boss gets on paper
- Bilingual narrative output (English / Hindi)

