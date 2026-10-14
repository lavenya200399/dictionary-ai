# Dictionary: answers from any data

A data-agnostic analytics app for business users. Connect a database, upload files or point at a REST API, and Dictionary
maps the data, builds a live dashboard, flags what changed, and answers questions in plain English.
It runs in the browser on desktop and phone, and installs as an app (PWA: "Add to Home Screen").

## Run it
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # add GEMINI_API_KEY (optional, see below)
python run.py                                          # open http://127.0.0.1:8000
```
Click **Try sample data** to load your ride-share CSVs, or **Connect data** to add your own. You can connect a database, upload files, fetch JSON from a REST API, or import static HTML tables from a public web page.

Everything except natural-language questions and narrated briefs works **without** an API key
(profiling, relationship discovery, Autopilot dashboard, Insight Radar, Constellation, Board, glossary).

## What makes it different
- **Autopilot:** the dashboard builds itself from whatever schema you connect: KPIs, trends, breakdowns, cross-table charts, distributions.
- **Insight Radar:** deterministic anomaly, trend, concentration, correlation and data-quality findings. Every card is clickable ("Ask why →").
- **Click-to-ask:** click any bar or slice on any chart to start a question about it.
- **Trust Receipt:** every answer shows its SQL, the safety checks it passed, rows, runtime and tables used.
- **Data Constellation:** an interactive map of your tables and the relationships Dictionary discovered (foreign keys, matching names, matching values).
- **Quality Studio:** preview null handling, deduplication, text standardization, and grouped aggregates, then download a transformed CSV without modifying the source.
- **Data contracts:** define required and unique fields, allowed values, and expected types. Save a schema baseline to flag later table, column, or inferred type changes.
- **Evidence investigation:** compare affected rows with unaffected rows to surface numeric and category signals. These are associations for investigation, not causal conclusions.
- **Business glossary (semantic layer):** define "active rider" once; it is applied to every question.
- **Live Board:** pin any chart or answer; it re-runs against live data each time.
- **Executive brief:** one-click narrated summary, printable to PDF.

## Data quality (the "Quality" tab)
One click runs a broad set of checks and gives a 0-100 score plus Completeness / Uniqueness / Validity / Consistency sub-scores:
missing and blank values, duplicate rows and keys, extreme outliers, negative amounts, future dates, end-before-start dates,
orphaned foreign keys, stray spaces, inconsistent casing, numbers stored as text, constant columns and stale data.
Duplicate and negative-value counts scan the full table; outlier thresholds are estimated from up to 50,000 rows and then counted across the full table. Every finding has an **Inspect rows** button that shows offending records, plus a suggested fix.
The Quality tab also contains a transformation preview/download studio, data-contract editor, saved schema baseline, and evidence-based comparisons for selected findings.

Web page import supports tables present in the page's HTML; JavaScript-rendered and sign-in-only pages are not supported. Private/internal addresses remain blocked by default.

## The Dictionary tab
An auto-generated data dictionary: every table and column in plain language, searchable, A-Z indexed, exportable as CSV.

## Safety model
SQL written by the model is parsed with `sqlglot` and must be a single read-only query on known tables, with no unsafe
functions or system tables. A row cap and timeout are enforced, and Postgres/MySQL sessions are set read-only.
Use a **read-only database user** as well. API sources block private/internal addresses unless `ALLOW_PRIVATE_URLS=1`.
No authentication is built in: put it behind your SSO / reverse proxy before exposing it beyond your machine.

## Layout
`server/engine.py` sources, profiling, autopilot, guarded SQL · `server/llm.py` Gemini pipeline · `server/main.py` API · `web/index.html` the app.
Interactive API docs: `/api/docs`.
