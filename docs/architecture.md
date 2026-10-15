# Architecture

<p align="center"><img src="assets/architecture.svg" alt="Dictionary system architecture" width="100%"></p>

Dictionary is a layered system. Data flows **up** (sources → connectors → engines → API → browser); requests flow **down**.
The one rule that shapes everything: **the AI never touches your data directly.** Everything it writes goes through a deterministic gate first.

## Layers

| # | Layer | Responsibility | Where |
|---|-------|----------------|-------|
| 1 | Client | Single-page app: 7 views, charts, force-graph lineage map, installable PWA | `web/index.html` |
| 2 | API | 26 REST endpoints, validation (Pydantic), error mapping, serves the SPA | `server/main.py` |
| 3 | Intelligence | Profiling, lineage discovery, autopilot dashboards, quality + contracts + drift, transforms, investigation, AI Ask | `server/engine.py`, `server/llm.py` |
| 4 | Safety and read path | Guarded SQL gate for any AI-written / pinned / inspected query; trusted engine-built queries for analytics | `engine.check_sql`, `engine.run` |
| 5 | Data access | Connectors (SQLAlchemy / pandas), ingestion guards, workspace state | `engine.add_*`, `workspace/` |
| 6 | Sources | Databases, files, REST APIs, public web tables, bundled sample | n/a |

## System view

```mermaid
flowchart TB
  subgraph Client["Browser: single-page app (PWA)"]
    UI["Home · Autopilot · Ask · Quality<br/>Constellation · Board · Dictionary"]
  end
  subgraph API["FastAPI: 26 REST endpoints"]
    R["Validation · error mapping · static hosting"]
  end
  subgraph Brain["Intelligence"]
    P["Profiler"] --> L["Lineage discovery"]
    P --> A["Autopilot"]
    P --> Q["Quality · Contracts · Drift"]
    P --> T["Transform · Investigate"]
    P --> K["Ask AI"]
  end
  LLM["Groq LLM<br/>gpt-oss-120b"]
  GATE{{"Guarded SQL gate<br/>7 deterministic checks"}}
  CONN["Connectors + guards<br/>SQLAlchemy · pandas"]
  WS[("Workspace<br/>per-source SQLite + JSON state")]
  SRC["Databases · Files · REST APIs<br/>Web tables · Sample"]
  UI <-->|HTTPS · JSON| R
  R --> Brain
  K <-->|schema summary, plan, narration| LLM
  K --> GATE
  GATE --> WS
  Brain -->|trusted engine queries| WS
  SRC --> CONN --> WS
```

## Request lifecycle: one question

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant UI as Browser
  participant API as FastAPI
  participant AI as llm.ask()
  participant LLM as Groq
  participant Gate as SQL gate
  participant DB as Data source
  U->>UI: "What explains the drop in users in June?"
  UI->>API: POST /api/sources/{id}/ask
  API->>AI: question + last two queries
  AI->>AI: build schema context (profile, links, glossary)
  AI->>LLM: prompt with schema summary
  LLM-->>AI: JSON plan {sql, title, approach, followups}
  AI->>Gate: validate SQL
  alt unsafe or invalid
    Gate-->>AI: Blocked / BadSQL
    AI-->>UI: explanation + Trust Receipt (blocked)
  else safe
    Gate->>DB: read-only query, row cap, timeout
    DB-->>Gate: rows
    Gate-->>AI: rows + passed checks
    AI->>LLM: at most 15 rows, narrate
    LLM-->>AI: plain-English answer
    AI-->>UI: answer + auto-chart + Trust Receipt
  end
```

If a query is merely wrong (unknown table, bad column) the error is sent back to the model once for self-correction; a *blocked* query is never retried.

## Data model of the sample dataset (discovered, not declared)

The bundled ride-share data has **no declared foreign keys**; Dictionary finds all 8 relationships itself.

```mermaid
erDiagram
  users ||--o{ rides : "rider_id / driver_id"
  rides ||--o{ payments : ride_id
  users ||--o{ payments : user_id
  rides ||--o{ ratings : ride_id
  users ||--o{ ratings : "rider_id / driver_id"
  users ||--o{ vehicles : driver_id
```

Relationship discovery runs three strategies in order: declared foreign keys (via SQLAlchemy reflection), identical column names against a unique key,
then **value overlap** (≥95 % of sampled child values exist in a candidate parent key, ties broken by coverage).

## Ingestion

| Source | Mechanism | Guards |
|--------|-----------|--------|
| Database URL | SQLAlchemy engine, queried in place | read-only session (Postgres/MySQL), file opened read-only (SQLite) |
| Files | pandas → cleaned column names, ISO dates → one SQLite file per source | 100 MB limit |
| REST API | `requests` → `json_normalize` → SQLite | private/loopback/link-local IPs blocked, no redirects |
| Web tables | `pandas.read_html` (lxml) on static HTML | 10 MB page cap, max 20 tables, same IP guard |
| Sample | bundled CSVs | n/a |

## Workspace state

| File | Contents |
|------|----------|
| `workspace/sources.json` | source registry (connection strings stay server-side) and business glossary |
| `workspace/<id>.db` | per-source SQLite for files, APIs, web tables and the sample |
| `workspace/contracts.json` | data contracts: required, unique, allowed values, expected types |
| `workspace/schema_baselines.json` | saved schemas for drift detection |
| `workspace/pins.json` | board pins (SQL is re-run live; no results are cached) |

## Design decisions and trade-offs

See the table in the [README](../README.md#engineering-decisions).
