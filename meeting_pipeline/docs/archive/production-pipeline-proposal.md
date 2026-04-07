# Production Municipal Data Pipeline — Proposal & Implementation Guide

## What We're Building

A production service that collects municipal government data for multiple cities, derives structured JSON analysis from it, and makes that data available to product features via gp-api.

The briefing POC proved this works for 3 NC cities at ~$0.04-0.06/city. This doc covers how to move from a local Python pipeline to a deployed, multi-repo production system.

### Initial Rollout: 7 Cities Across 3 States

| City | State | Legislative System | API Slug / Endpoint | Budget Data Source |
|------|-------|-------------------|--------------------|--------------------|
| Charlotte | NC | Legistar | `charlottenc` | NC LINC (Opendatasoft) |
| Raleigh | NC | BoardDocs | `go.boarddocs.com/nc/raleigh` | NC LINC (Opendatasoft) |
| Columbus | OH | Legistar | `columbus` | OH Auditor (Spreadsheet download) |
| Cleveland | OH | Legistar | `cityofcleveland` | OH Auditor (Spreadsheet download) |
| Dallas | TX | Legistar | `cityofdallas` | TX data.texas.gov (Socrata) |
| Austin | TX | Legistar | `austintexas` | TX data.texas.gov (Socrata) |
| San Antonio | TX | PrimeGov | `sanantonio.primegov.com` | TX data.texas.gov (Socrata) |

3 additional cities TBD. Architecture supports adding new cities via config + API.

**Note on San Antonio:** Migrated from Legistar to PrimeGov in ~2021. PrimeGov has a JSON API for meeting lists but agenda item detail requires HTML scraping. Alternative Legistar cities in TX: Fort Worth (`fortworthgov`) and El Paso (`elpasotexas`).

**Note on Legistar slugs:** Several cities use non-obvious slugs. Charlotte is `charlottenc` (not `charlotte`), Cleveland is `cityofcleveland`, Dallas is `cityofdallas`, Austin is `austintexas`.

---

# Part 1: Architecture Proposal

## When Does City Data Become Usable?

The POC pipeline has 3 stages. Each produces progressively more useful data:

### Stage 1: Raw Collection (scripts 01-03, 06)

Collects raw data from public APIs and stores as JSON files.

| What You Get | Example | Directly Usable? |
|-------------|---------|-----------------|
| Legislative matters | 686 items with titles, types, statuses, dates | Partially — you can list/search, but no categorization |
| Meeting agendas | Agenda items with action text, motion details | Partially — raw text, not structured insights |
| Vote records | Roll-call votes with member names, yes/no | Yes — can show vote history directly |
| Budget data | 44 years of revenue, expenditure, tax rates | Yes — can chart trends directly |
| PDF text | Extracted text from 870 staff reports | No — unstructured text, needs analysis to be useful |
| Constituent data | 642K voters, demographics, 15 issue scores | Yes — can show demographics and issue priorities |

**At this stage:** You have raw materials. Vote records, budget numbers, and constituent data are directly usable. Legislative matters and PDF text need analysis to become product-ready.

### Stage 2: LLM Analysis (script 04 — 6 passes with Gemini)

Runs structured LLM analysis over the raw data. Each pass produces a clean JSON output with a defined schema.

| Pass | Output | What It Gives Product Features |
|------|--------|-------------------------------|
| Pass 1: Legislative Overview | Topics, matter counts, key matters per topic | "What does this city council spend time on?" — ready for a topic breakdown UI |
| Pass 2: Vote Analysis | Vote patterns, unanimous/contested, frequent movers | "How does this council vote?" — ready for a voting patterns component |
| Pass 3: Budget Analysis | Revenue/expenditure trends, tax rate history, fiscal pressures | "What's the financial picture?" — ready for budget dashboard charts |
| Pass 4: Document Summaries | Top 50 documents with key issues, fiscal impact, recommendations | "What are the important staff reports about?" — ready for a document browser |
| Pass 5: Committee Analysis | Committee profiles, workload, composition | "What committees exist and what do they do?" — ready for a committee directory |
| Pass 6: Synthesis | Executive summary, key themes, priorities, knowledge gaps | "What are the big takeaways?" — ready for a city overview card |

**At this stage:** Each analysis pass produces structured JSON that can directly power UI components. This is where raw city data becomes a product.

**Cost:** ~$0.02-0.04 per city for all 6 passes (Gemini 2.5 Flash).

### Stage 3: Data Joining (scripts 06b, 07, 09 — no LLM needed)

Cross-references analysis output with constituent data.

| Script | Output | What It Gives Product Features |
|--------|--------|-------------------------------|
| Topic-to-Issue Mapping (06b) | Maps council topics → voter issue columns | Enables the mismatch analysis |
| Council vs. Constituent (07) | Topic-by-topic: % of agenda time vs. voter priority score | "Where is the council misaligned with voters?" — the CPO's core insight |
| Quick Wins (09) | Concrete actions tied to gap areas with specific matter references | "What should this official focus on?" — ready for a recommendations UI |

**At this stage:** You have actionable, personalized insights that differentiate the product.

### Recommendation

**Build all 3 stages.** Stage 1 alone gives you raw data that's only partially usable. Stages 2-3 are what turn raw government records into a product. The LLM cost is negligible ($0.04-0.06/city total) and the derived JSON is what makes city data valuable.

---

## Where Should This Live?

### Recommendation: Hybrid — gp-api (TypeScript) + gp-ai-projects (Python sidecar)

**Why not all in gp-ai-projects?**
The consumers of city data are product features in gp-api. If the pipeline lives entirely in gp-ai-projects, every product feature needs cross-repo API calls or S3 reads to access city data. Putting the pipeline in gp-api means data flows directly into PostgreSQL/Prisma where product features already live.

**Why not all in gp-api?**
PDF extraction requires PyMuPDF + pdfplumber — Python C-extension libraries with no JavaScript equivalent for table extraction. This is the one hard blocker.

**The split:**

| Component | Where It Runs | Language | Why |
|-----------|--------------|----------|-----|
| Legistar collector | **Lambda** (`municipal-legistar`) | TypeScript | One Lambda per collector type. 10 cities call the same Lambda in parallel — each invocation handles one city. |
| BoardDocs collector | **Lambda** (`municipal-boarddocs`) | TypeScript | Same pattern. Separate Lambda means cheerio is only bundled where needed. |
| PrimeGov collector | **Lambda** (`municipal-primegov`) | TypeScript | Same pattern. HTML scraping deps isolated. |
| NC Budget collector | **Lambda** (`municipal-budget-opendatasoft`) | TypeScript | Lightweight Opendatasoft API calls, ~30s per city. |
| TX Budget collector | **Lambda** (`municipal-budget-socrata`) | TypeScript | Socrata/SoQL queries, ~30s per city. |
| OH Budget collector | **Lambda** (`municipal-budget-ohio-auditor`) | TypeScript | Downloads + parses Excel. xlsx dep isolated to this Lambda. |
| Scheduling & fan-out | **EventBridge + Lambda** (`municipal-fanout`) | TypeScript | EventBridge cron triggers fan-out. Fan-out invokes the correct collector Lambdas per city (async). |
| Collection completion handler | **gp-api** | TypeScript | SQS consumer saves metadata to Prisma, same pattern as `POLL_ANALYSIS_COMPLETE`. |
| Constituent demographics | **people-api** | TypeScript | Already exists — `DistrictStats` has pre-computed demographic buckets. No new code needed. |
| Constituent issue scores (Haystaq) | **Lambda** | TypeScript | Databricks REST SQL API for issue priority scores only. |
| PDF extraction | **gp-ai-projects** | Python | pdfplumber has no JS equivalent. Small Fargate task. |
| LLM analysis (6 passes) | **Lambda** | TypeScript | google-genai has a JS SDK (`@google/genai`). Zod replaces Pydantic. Each pass ~1-2 min. |
| Data joining (mismatch, quick wins) | **Lambda** | TypeScript | Pure math/data operations. Trivial. |
| City data API endpoints | **gp-api** | TypeScript | Serve data to webapp and other consumers. |

**Cross-repo integration:** PDF extraction follows the existing `POLL_ANALYSIS_COMPLETE` pattern — gp-ai-projects writes results to S3, sends an SQS message to gp-api, and gp-api's queue consumer processes it. This pattern is already proven in production.

### Why Per-Collector Lambdas

**The problem with running collectors in gp-api:**
- Collection is batch work (1-3 min per city, hundreds of API calls) — blocks the API process
- No isolation — a collector crash or OOM takes down the API
- No per-city parallelism — sequential processing, can't scale to 10+ cities
- No independent retry — if Charlotte fails, can't retry just Charlotte

**Why one Lambda per collector type (not one monolithic collector Lambda):**
- **10 cities calling Legistar** = 10 parallel invocations of the same `municipal-legistar` Lambda. Clean.
- **No routing logic** — each Lambda IS the collector. No `if (config.system === 'legistar')` dispatch.
- **Independent dependencies** — cheerio only in BoardDocs Lambda, xlsx only in OH Budget Lambda. Smaller packages.
- **Independent scaling** — legislative collectors (1-3 min) and budget collectors (10-30s) have different timeout/memory needs.
- **Easy to add** — new collector = new Lambda in the Pulumi loop + new collector file. Nothing else changes.

**Fan-out Lambda is the router:**
- Reads city config, knows Charlotte uses Legistar + Opendatasoft
- Invokes `municipal-legistar` Lambda async with Charlotte's config
- Invokes `municipal-budget-opendatasoft` Lambda async with Charlotte's config
- Both run in parallel, independently

**Progressive completion:**
- Each collector sends its own completion message (`MUNICIPAL_LEGISLATIVE_COMPLETE` or `MUNICIPAL_BUDGET_COMPLETE`)
- gp-api tracks them independently — product features can show legislative data as soon as it arrives, without waiting for budget
- No need to count completions or wait for "all done"

**gp-api stays lean:**
- Prisma models for config and tracking
- API endpoints for triggering and reading data
- SQS consumer for handling completion messages
- Product features query city data through Prisma

**Infrastructure:** All collector Lambdas defined in a Pulumi loop in `deploy/index.ts`. Adding a new collector type = adding one entry to the array.

---

## TypeScript vs Python: The Full Picture

### What Ports Cleanly to TypeScript

| Python Component | TypeScript Equivalent | Effort |
|-----------------|----------------------|--------|
| `httpx` (HTTP client) | `fetch` / `axios` | Drop-in |
| `HTMLParser` (stdlib) | `cheerio` | Nearly identical API |
| `json` (stdlib) | Built-in `JSON` | Drop-in |
| `asyncio` | `async/await` + `Promise.all` | Native to JS |
| `pydantic` models | `zod` schemas | Same concept, different syntax |
| `google-genai` SDK | `@google/genai` SDK | Official Google SDK for Node.js |
| `databricks-sql` | Databricks REST SQL API | HTTP calls instead of Python SDK |
| `pathlib` / file I/O | S3 reads/writes via `S3Service` | Already exists in gp-api |

### What Must Stay Python

| Component | Why | Mitigation |
|-----------|-----|------------|
| `pdfplumber` (table extraction) | Detects table structure from visual layout — no JS library does this | Small Python Fargate task, triggered by S3 event |
| `PyMuPDF` / `fitz` (text extraction) | Mozilla's `pdf.js` can do text extraction in JS, but pdfplumber needs it for table context | Bundled with pdfplumber in the same Fargate task |

**The Python surface area is minimal:** One Fargate task (~235 lines of existing code) that reads PDFs from S3, extracts text + tables, writes JSON back to S3. Everything else is TypeScript.

---

## Data Storage

### Dual Storage: S3 (raw) + PostgreSQL JSONB (derived)

| Data | Where | Why |
|------|-------|-----|
| Raw collector output (matters.json, events.json, PDFs) | S3 | Cheap, preserves full data for reprocessing, audit trail |
| Extracted PDF text (JSON per document) | S3 | Large volume (~9M chars for Charlotte), accessed on-demand |
| Analysis pass results (6 JSON files) | PostgreSQL JSONB + S3 | JSONB for fast product queries, S3 as backup |
| Derived data (mismatch, quick wins) | PostgreSQL JSONB | Small, frequently queried by product features |
| City config | PostgreSQL JSON column | Part of the Municipality model |

### S3 Key Structure

Data is organized by city, then category, then **collection date**. Each weekly full collection creates a new dated folder — an immutable snapshot of what was collected that week. Daily syncs update the most recent weekly folder (merging in newly published agendas).

```
municipal-data-{env}/
  {city_slug}/
    legislative/
      2026-03-02/                    ← weekly full collection (immutable snapshot)
        _manifest.json               ← describes all files in this folder
        bodies.json
        events.json
        event_items.json
        matters.json
        votes.json
        persons.json
        pdfs/
          {attachmentId}.pdf
      2026-03-09/                    ← next weekly full (new dated folder)
        _manifest.json
        bodies.json
        events.json
        ...
    budget/
      2026-03-02/
        _manifest.json
        government_fiscal.json
        property_tax_rate.json
      2026-03-09/
        _manifest.json
        ...
    extracted/                       ← (Phase 5+) PDF extraction output
      2026-03-02/
        {attachmentId}.json
    analysis/                        ← (Phase 5+) LLM analysis output
      2026-03-02/
        pass1_legislative_overview.json
        pass2_vote_analysis.json
        pass3_budget_analysis.json
        pass4_document_summaries.json
        pass5_committee_analysis.json
        pass6_synthesis.json
        topic_to_issue_map.json
        council_vs_constituent.json
        quick_wins.json
    constituent/                     ← (Phase 5+) Haystaq issue scores
      2026-03-02/
        issue_scores.json
        zip_breakdown.json
```

**Key rules:**
- `{city_slug}` is the top level — e.g., `charlotte-nc`, `dallas-tx`
- Each category (`legislative/`, `budget/`) has dated folders: `YYYY-MM-DD/` (the date the collection ran)
- **Weekly full** creates a new dated folder each time
- **Daily sync** updates files in the most recent dated folder (e.g., updates `events.json` with newly published agendas) and updates `_manifest.json` with `lastSyncedAt`
- **PipelineRun in Prisma** stores the S3 prefix (e.g., `charlotte-nc/legislative/2026-03-09/`) — this is the index. No S3 listing needed to find current data.
- **S3 lifecycle policy** expires old dated folders after 90 days (~12 weekly snapshots retained)

### Manifest File (`_manifest.json`)

Each dated collection folder includes a `_manifest.json` that describes its contents. This allows any consumer to understand what's in the folder without opening every file.

```json
{
  "citySlug": "charlotte-nc",
  "category": "legislative",
  "collectionType": "full",
  "collectedAt": "2026-03-09T06:02:14Z",
  "lastSyncedAt": null,
  "collector": "municipal-legistar",
  "files": {
    "bodies.json": { "records": 21 },
    "events.json": { "records": 847, "dateRange": ["2020-01-06", "2026-10-15"] },
    "event_items.json": { "records": 3200 },
    "matters.json": { "records": 12450 },
    "votes.json": { "records": 3200 },
    "persons.json": { "records": 45 }
  },
  "pdfs": 128
}
```

After a daily sync updates the folder:
```json
{
  "citySlug": "charlotte-nc",
  "category": "legislative",
  "collectionType": "full",
  "collectedAt": "2026-03-09T06:02:14Z",
  "lastSyncedAt": "2026-03-11T06:01:30Z",
  "collector": "municipal-legistar",
  "files": {
    "bodies.json": { "records": 21 },
    "events.json": { "records": 852, "dateRange": ["2020-01-06", "2026-10-15"] },
    "event_items.json": { "records": 3215 },
    "matters.json": { "records": 12450 },
    "votes.json": { "records": 3200 },
    "persons.json": { "records": 45 },
    "upcoming_events.json": { "records": 12, "dateRange": ["2026-03-11", "2026-03-25"], "syncedAt": "2026-03-11T06:01:30Z" }
  },
  "pdfs": 128
}
```

### Completion Message Format

Each collector sends a completion message to gp-api SQS that includes the S3 prefix it wrote to:

```json
{
  "type": "municipalLegislativeComplete",
  "citySlug": "charlotte-nc",
  "s3Prefix": "charlotte-nc/legislative/2026-03-09/",
  "collectedAt": "2026-03-09T06:02:14Z",
  "collectionType": "full",
  "fileCount": 7,
  "recordCounts": {
    "bodies": 21,
    "events": 847,
    "matters": 12450,
    "votes": 3200
  }
}
```

gp-api stores `s3Prefix` on the `MunicipalPipelineRun` record. When anything needs Charlotte's legislative data, it queries PipelineRun for the latest completed run's prefix — no S3 listing needed.

### How Data Is Found

The lookup flow for any consumer:
1. **Query Prisma** → get the latest `MunicipalPipelineRun` for this city + category → has `s3Prefix`
2. **Read `_manifest.json`** at that prefix → know exactly what files exist, record counts, date ranges
3. **Read only the files you need** — never scan the folder
```

---

## Pipeline Flow

```
1. Trigger
   ├── EventBridge cron (daily agenda sync / weekly full collection)
   ├── API call (POST /v1/municipal-data/configs/:slug/collect)
   └── Either way → fan-out Lambda reads active city configs
   │
   ▼
2. Fan-out Lambda (the router)
   ├── For each active city, reads config to determine collector types
   ├── Invokes the correct legislative Lambda async (e.g., municipal-legistar)
   ├── Invokes the correct budget Lambda async (e.g., municipal-budget-opendatasoft)
   └── All invocations run in parallel — 7 cities × 2 collectors = 14 parallel Lambdas
   │
   ▼
3. Collector Lambdas (per collector type, per city, all parallel)
   │
   ├── municipal-legistar (Charlotte, Columbus, Cleveland, Dallas, Austin)
   │   ├── Fetches bodies, events, matters, votes (paginated OData)
   │   ├── Downloads PDF attachments
   │   ├── Writes raw JSON + PDFs to S3
   │   └── Sends MUNICIPAL_LEGISLATIVE_COMPLETE to gp-api SQS
   │
   ├── municipal-boarddocs (Raleigh)
   │   ├── POSTs to undocumented BD- endpoints, parses HTML with cheerio
   │   ├── Writes meetings, agendas, items to S3
   │   └── Sends MUNICIPAL_LEGISLATIVE_COMPLETE to gp-api SQS
   │
   ├── municipal-budget-opendatasoft (Charlotte, Raleigh)
   │   ├── Queries NC LINC API
   │   ├── Writes budget JSON to S3
   │   └── Sends MUNICIPAL_BUDGET_COMPLETE to gp-api SQS
   │
   └── municipal-budget-socrata (Dallas, Austin, San Antonio)
       ├── Queries data.texas.gov + city portals
       ├── Writes budget JSON to S3
       └── Sends MUNICIPAL_BUDGET_COMPLETE to gp-api SQS
   │
   ▼
4. gp-api SQS consumer (completion handler)
   ├── Receives MUNICIPAL_LEGISLATIVE_COMPLETE → updates PipelineRun, lastCollectedAt
   ├── Receives MUNICIPAL_BUDGET_COMPLETE → updates PipelineRun
   └── (Phase 5+) When both complete, triggers analysis pipeline
   │
   ▼
5. (Phase 5+) S3 PDF upload triggers Lambda → Fargate (Python, gp-ai-projects)
   ├── Extract text + tables from PDFs
   ├── Write extracted JSON to S3
   └── Send MUNICIPAL_PDF_EXTRACTION_COMPLETE to SQS
   │
   ▼
6. (Phase 5+) Analysis Lambda (per city)
   ├── Run 6-pass LLM analysis (Gemini via @google/genai)
   ├── Run topic-to-issue mapping, mismatch analysis, quick wins
   ├── Save analysis JSON to S3 + PostgreSQL JSONB
   └── Send MUNICIPAL_ANALYZE_COMPLETE to gp-api SQS
```

**Progressive data availability:** Legislative and budget data arrive independently. Product features can show legislative data (matters, votes, agendas) as soon as `MUNICIPAL_LEGISLATIVE_COMPLETE` arrives, without waiting for budget. Budget data appears when its collector finishes.

**Graceful degradation:** If PDF extraction fails or is slow, the pipeline can still run analysis passes 1-3 and 5-6 (which don't depend on extracted PDF text). Pass 4 (document summaries) waits for extraction.

---

## Sync & Scheduling: Keeping Data Fresh

### Why Sync Matters

Meeting agendas are published 1-6 days before the meeting. A one-time collection misses future agendas. Different cities behave differently — Dallas pre-schedules events 6+ months out (but agenda items are empty until ~5 days before), while Charlotte only has events a few weeks ahead.

**Reliable indicators that an agenda has been published:**
- `EventAgendaFile` is non-null (PDF URL exists)
- `/events/{id}/eventitems` returns a non-empty array
- Do NOT rely on `EventAgendaStatusName` — it shows "Final" for everything, even empty future events

### Two Sync Cadences

**1. Daily Agenda Sync (lightweight)**
- Runs every morning (e.g., 6 AM UTC)
- For each active city, fetch events in the next 14 days
- Check which events have new/updated agenda items since last sync
- Download agenda items + attachments for newly published agendas
- Fast: only queries recent/upcoming events, not full history

**2. Weekly Full Collection (heavy)**
- Runs once per week (e.g., Sunday 2 AM UTC)
- Full collection run for each city (all matters, events, votes, budget data)
- Updates all raw data in S3
- Catches any data that the daily sync might miss

### Implementation: EventBridge + Fan-out + Per-Collector Lambdas

Each collector type is its own Lambda. The fan-out Lambda is the router — it knows which collectors each city needs and invokes them directly (async).

```
┌──────────────────────┐     ┌────────────────────┐     ┌─────────────────────────────────────┐
│ EventBridge Cron     │────▶│ Fan-out Lambda     │────▶│ Per-collector Lambdas (async invoke) │
│ Daily 6AM / Wkly 2AM│     │ reads active cities │     │                                     │
└──────────────────────┘     │ invokes collectors  │     │  municipal-legistar (× 5 cities)    │
                             └────────────────────┘     │  municipal-boarddocs (× 1 city)     │
                                                        │  municipal-budget-opendatasoft (× 2) │
                                                        │  municipal-budget-socrata (× 3)      │
                                                        │  municipal-budget-ohio-auditor (× 2)  │
                                                        └─────────────────────────────────────┘
                                                                         │
                                                                         ▼
                                                                   Writes to S3
                                                                         │
                                                                         ▼
                                                                 ┌────────────┐
                                                                 │ gp-api SQS │
                                                                 │ consumer   │
                                                                 └────────────┘
```

**EventBridge rules (Pulumi in `deploy/index.ts`):**
- Daily agenda sync: `cron(0 6 * * ? *)` → fan-out Lambda with `{ collectionType: "agenda_sync" }`
- Weekly full collection: `cron(0 2 ? * SUN *)` → fan-out Lambda with `{ collectionType: "full" }`

**Fan-out Lambda** (~30 lines): reads active city configs from DB. For each city, invokes the correct legislative Lambda + budget Lambda directly (async `InvocationType: 'Event'`). No intermediate SQS queue needed.

**Collector Lambdas** (per type): each receives `{ citySlug, collectionType, config }` directly from the fan-out. Runs the collector, writes to S3, sends completion message to gp-api SQS. No routing logic — each Lambda IS the collector.

**API-triggered collection** also works: `POST /v1/municipal-data/configs/:slug/collect` invokes the collector Lambdas directly (same async pattern as fan-out).

For Legistar, the daily sync query is:
```
GET /events?$filter=EventDate ge datetime'{today}' and EventDate le datetime'{today+14}'
```

For BoardDocs, call `BD-GetMeetings` for the City Council committee and check for new meetings.

---

## Budget Data Sources by State

### North Carolina: LINC/OSBM (Opendatasoft)
- **API:** `https://linc.osbm.nc.gov/api/explore/v2.1/catalog/datasets`
- **Datasets:** `government` (47+ fiscal variables), `property-tax-rate`
- **Filter:** `where=area_name='Charlotte'` (or `'Raleigh'`)
- **Coverage:** All NC municipalities. No auth required.
- **Quality:** Excellent. 44 years of data, revenue + expenditure + tax rates.

### Ohio: Auditor of State (Spreadsheet Downloads)
- **Source:** `ohioauditor.gov/references/SummarizedAnnualFinancialReports`
- **Format:** Excel spreadsheets by entity type and year
- **Filter:** Download "Cities" spreadsheet, filter rows for Columbus/Cleveland
- **Coverage:** All Ohio cities. No API — file downloads only.
- **Quality:** Comprehensive but requires parsing. Data from 2016-2025.

### Texas: data.texas.gov (Socrata) + City Portals
- **Statewide debt/tax:** `data.texas.gov/resource/dyv5-3bjd.json` — SoQL filter: `$where=governmentname='Dallas' AND governmenttype='CITY'`
- **Austin budget:** `data.austintexas.gov/resource/8c6z-qnmj.json` (eCheckbook)
- **Dallas budget:** `www.dallasopendata.com` (Socrata)
- **San Antonio:** `data.sanantonio.gov` + `sanantoniotx.opengov.com/transparency`
- **Coverage:** Statewide for debt/tax. City-specific for operating budgets.

---

## Effort Estimates

Estimates assume AI-assisted development (engineer + Claude Code pairing).

| Phase | What | Effort | Dependencies |
|-------|------|--------|-------------|
| **Phase 1: Foundation** | Prisma models, NestJS module, S3 bucket, city config schema, seed 7 cities | 2-3 days | None |
| **Phase 2: Lambda Infrastructure** | Per-collector Lambdas (Pulumi loop) + fan-out Lambda + EventBridge rules + DLQ | 1-2 days | Phase 1 |
| **Phase 3: Legislative Collectors** | Legistar (6 cities), BoardDocs (Raleigh), PrimeGov (San Antonio if keeping) | 2-3 days | Phase 2 |
| **Phase 4: Budget Collectors** | NC Opendatasoft, TX Socrata, OH Spreadsheet parser | 1-2 days | Phase 2 |
| **Phase 5: API + Completion Handling** | REST endpoints, SQS completion handler, gp-api integration | 1-2 days | Phases 3-4 |
| **Phase 6: Analysis Pipeline** | Port 6 LLM passes + data joining to TypeScript Lambda (when ready) | 3-4 days | Phase 5 |
| **Phase 7: PDF Sidecar + Constituent Data** | Terraform module, Python CLI wrapper, people-api + Databricks integration | 2-3 days | Phase 2 |
| **Total (ingestion only, Phases 1-5)** | | **~1 week** | |
| **Total (full pipeline, all phases)** | | **~2 weeks** | |

**Recommended approach:** Build Phases 1-4 first (data ingestion for all 7 cities with sync). This gives the product team queryable city data immediately. Add analysis pipeline (Phases 5-6) when product features need derived insights.

**Why these estimates are compressed:** The POC Python code provides working reference implementations for every collector, LLM pass, and data transform. AI-assisted porting from Python → TypeScript with a working reference is significantly faster than writing from scratch — the logic, edge cases, and schemas are already solved.

---

# Part 2: Implementation Guide

## Integration with Existing Models

Before defining new models, here's what already exists across our repos:

### What We Already Have

| Existing Model | Repo | What It Stores | Relevance |
|---------------|------|---------------|-----------|
| **Place** | election-api | Geographic entities (cities, counties, states) with demographics, hierarchy, Census GEOID, BallotReady link | **This IS the municipality.** Charlotte is a Place with population, income, etc. |
| **District** | election-api | L2 voter districts (state + L2DistrictType + L2DistrictName) | Links positions to voter geography. City council districts live here. |
| **Position** | election-api | BallotReady offices (brPositionId, name, linked to District) | Knows what elected offices exist in a Place. |
| **Race** | election-api | Electoral races linked to Places, with candidacies | Knows who's running for what, when. |
| **District** | people-api | Mirror of election-api District (same UUIDs) with voter links | Maps individual L2 voters to districts. |
| **DistrictStats** | people-api | Pre-computed demographic buckets per district (age, income, education, etc.) | **Already has the constituent demographics** the POC collects from Haystaq. |
| **Campaign** | gp-api | References `placeId` (election-api Place UUID) | Established pattern for linking to geographic entities. |
| **Organization** | gp-api | References `positionId` and `overrideDistrictId` (election-api UUIDs) | Established pattern for cross-repo position/district references. |

### What We Don't Have (and need to build)

| Data | Why It's New |
|------|-------------|
| Legislative collector config (Legistar client ID, BoardDocs site, etc.) | Pipeline-specific — not in any existing model |
| Raw legislative data (matters, events, votes) | New data source — not collected anywhere today |
| Budget data from LINC | New data source |
| Haystaq issue scores (hs_affordable_housing, hs_gun_control, etc.) | people-api has voter demographics but NOT Haystaq DNA issue priority scores |
| LLM analysis results (6 structured passes) | Entirely new |
| Council-vs-constituent mismatch, quick wins | Entirely new derived data |

### Key Insight: Demographics vs Issue Scores

The POC's Haystaq collector queries Databricks for two things:
1. **Demographics** (party breakdown, age, gender) — **people-api `DistrictStats` already has this** per district with pre-computed buckets
2. **Issue scores** (Haystaq DNA scores like affordable_housing, gun_control, immigration) — **people-api does NOT have these**. They live in separate Databricks tables (`stg_dbt_source__l2_s3_{state}_haystaq_dna_scores`).

So: use people-api for demographics, still query Databricks for issue scores.

---

## Prisma Models

New models for gp-api. The key difference from a standalone `Municipality` model: we reference election-api's `Place` instead of duplicating city name/state/demographics.

```prisma
// prisma/schema/municipalData.prisma

model MunicipalDataConfig {
  id                String    @id @default(uuid(7))

  // Link to election-api Place (the geographic entity — has name, state, demographics, hierarchy)
  placeId           String?   @unique @map("place_id")    // election-api Place UUID (e.g., Charlotte Place)

  // Fallback identifiers (for cities not yet in election-api, or for display without cross-service call)
  slug              String    @unique                      // "charlotte" — our internal identifier
  name              String                                 // "Charlotte, NC" — denormalized for display
  stateCode         String    @map("state_code")           // "nc"

  // Pipeline-specific config (not in Place)
  legislativeSystem String    @map("legislative_system")   // "legistar" | "boarddocs" | "escribemeetings"
  config            Json      @db.JsonB                    // Full city_config.json (API keys, endpoints, collector params)

  // Pipeline state
  isActive          Boolean   @default(true) @map("is_active")
  lastCollectedAt   DateTime? @map("last_collected_at")
  lastAnalyzedAt    DateTime? @map("last_analyzed_at")

  createdAt         DateTime  @default(now()) @map("created_at")
  updatedAt         DateTime  @updatedAt @map("updated_at")

  pipelineRuns      MunicipalPipelineRun[]
  dataSnapshots     MunicipalDataSnapshot[]

  @@map("municipal_data_config")
}

model MunicipalPipelineRun {
  id              String    @id @default(uuid(7))
  configId        String    @map("config_id")
  config          MunicipalDataConfig @relation(fields: [configId], references: [id])

  category        String                          // "legislative" | "budget"
  collectionType  String    @map("collection_type") // "full" | "agenda_sync"
  status          String    // "pending" | "collecting" | "extracting_pdfs" | "analyzing" | "complete" | "failed"
  startedAt       DateTime  @default(now()) @map("started_at")
  completedAt     DateTime? @map("completed_at")

  s3Prefix        String    @map("s3_prefix")     // e.g., "charlotte-nc/legislative/2026-03-09/"
  collectionDate  String    @map("collection_date") // e.g., "2026-03-09" — the dated folder name

  collectResult   Json?     @map("collect_result") @db.JsonB  // Collector summary stats (record counts, etc.)
  analysisResult  Json?     @map("analysis_result") @db.JsonB // Analysis summary stats
  error           Json?     @db.JsonB

  createdAt       DateTime  @default(now()) @map("created_at")
  updatedAt       DateTime  @updatedAt @map("updated_at")

  @@index([configId])
  @@index([configId, category, status])           // Fast lookup: latest completed run per city + category
  @@map("municipal_pipeline_run")
}

model MunicipalDataSnapshot {
  id              String    @id @default(uuid(7))
  configId        String    @map("config_id")
  config          MunicipalDataConfig @relation(fields: [configId], references: [id])

  dataType        String    @map("data_type")  // "legislative_overview" | "vote_analysis" | "budget_analysis" | etc.
  version         Int       @default(1)
  data            Json      @db.JsonB          // The analysis JSON

  s3Key           String?   @map("s3_key")     // Reference to full S3 data

  collectedAt     DateTime  @map("collected_at")
  createdAt       DateTime  @default(now()) @map("created_at")
  updatedAt       DateTime  @updatedAt @map("updated_at")

  @@unique([configId, dataType])
  @@index([configId])
  @@map("municipal_data_snapshot")
}
```

### Why `MunicipalDataConfig` instead of `Municipality`

- **Not a geographic entity** — that's election-api's `Place`. This is a pipeline configuration that says "for this city, use this collector with these params."
- **`placeId` links to Place** — same pattern Campaign already uses. Demographics, hierarchy, and Race data come from election-api, not duplicated here.
- **Denormalized `name`/`stateCode`** — avoids cross-service calls for display-only needs (listing active cities, Slack alerts, etc.). Kept in sync when config is created.
- **`slug` is our key** — simpler than Place's hierarchical slug (e.g., "charlotte" vs "nc/mecklenburg-county/charlotte"). Used in S3 paths and API routes.

## Code Structure

### gp-api Module (API + completion handling)

```
src/municipalData/
  municipalData.module.ts
  controllers/
    municipalData.controller.ts            # API endpoints (CRUD, trigger collection, read data)
  services/
    municipalData.service.ts               # Prisma CRUD for MunicipalDataConfig
    municipalPipeline.service.ts           # Handles SQS completion messages, updates Prisma
  schemas/
    cityConfig.schema.ts                   # Zod schema for city config (supports all systems)
    analysis/                              # (Phase 5+)
      legislativeOverview.schema.ts
      voteAnalysis.schema.ts
      budgetAnalysis.schema.ts
      documentSummaries.schema.ts
      committeeAnalysis.schema.ts
      synthesis.schema.ts
  types/
    municipalData.types.ts
```

### Lambda Functions (per-collector + fan-out)

Each collector type is its own Lambda with its own handler entry point. Same codebase, different handlers. No routing logic — the fan-out Lambda invokes the right one directly. Deployed via Pulumi loop alongside gp-api infrastructure.

```
lambdas/municipal-collectors/
  handlers/
    legistar.handler.ts                   # Entry point for municipal-legistar Lambda
    boarddocs.handler.ts                  # Entry point for municipal-boarddocs Lambda
    primegov.handler.ts                   # Entry point for municipal-primegov Lambda
    budget-opendatasoft.handler.ts        # Entry point for municipal-budget-opendatasoft Lambda
    budget-socrata.handler.ts             # Entry point for municipal-budget-socrata Lambda
    budget-ohio-auditor.handler.ts        # Entry point for municipal-budget-ohio-auditor Lambda
    haystaq.handler.ts                    # (Phase 5+) Entry point for municipal-haystaq Lambda
  collectors/
    legistar.collector.ts                 # 5-6 cities — Port of collectors/legistar.py
    boarddocs.collector.ts                # Raleigh — Port of collectors/boarddocs.py (cheerio)
    primegov.collector.ts                 # San Antonio — PrimeGov JSON API + HTML scraping
    budgetOpendatasoft.collector.ts       # NC (Charlotte, Raleigh) — Port of collectors/budget_linc.py
    budgetSocrata.collector.ts            # TX (Dallas, Austin, San Antonio) — SoQL queries
    budgetOhioAuditor.collector.ts        # OH (Columbus, Cleveland) — spreadsheet download + parse
    haystaqIssueScores.collector.ts       # (Phase 5+) Databricks REST API — issue scores ONLY
  shared/
    s3.ts                                 # S3 upload/download helpers (thin wrapper around AWS SDK)
    config.ts                             # City config types + loader
    http.ts                               # fetchPaginated, rate limiting, retry helpers
    completion.ts                         # Send completion message to gp-api SQS (includes s3Prefix)
    manifest.ts                           # Write _manifest.json to S3 (file list, record counts, timestamps)

lambdas/municipal-fanout/
  handler.ts                              # Reads active cities from DB, invokes collector Lambdas per city
```

Each handler is ~10 lines — unwrap the event, call the collector, send completion with the S3 prefix. Example:
```typescript
// handlers/legistar.handler.ts
import { collectLegistar } from '../collectors/legistar.collector';
import { sendCompletion } from '../shared/completion';

export async function handler(event: { citySlug: string; collectionType: string; config: any }) {
  const result = await collectLegistar(event.citySlug, event.config.legistarClient, process.env.S3_BUCKET!, event.collectionType);
  await sendCompletion('MUNICIPAL_LEGISLATIVE_COMPLETE', event.citySlug, {
    s3Prefix: result.s3Prefix,         // e.g., "charlotte-nc/legislative/2026-03-09/"
    collectionType: event.collectionType,
    recordCounts: { matters: result.matters, events: result.events, bodies: result.bodies },
  });
}
```

### Constituent Data: people-api + Haystaq Hybrid

Instead of a standalone Haystaq collector that duplicates what people-api already has:

| Data Need | Source | How |
|-----------|--------|-----|
| Voter demographics (party, age, gender, income, education) | **people-api DistrictStats** | Internal API call — already pre-computed per district |
| Haystaq issue priority scores (affordable housing, gun control, etc.) | **Databricks REST API** | Query `haystaq_dna_scores` tables — same data the POC uses |
| Zip code breakdown with issue scores | **Databricks REST API** | Still needed — people-api doesn't aggregate by zip + issue score |

The `constituentData.service.ts` combines both sources into the same JSON structure the analysis passes expect.

## Collector Porting Guide

Each Python collector maps to a TypeScript service. Here's what changes:

### Legistar (simplest port)

**Python (legistar.py):** `httpx.AsyncClient` → paginated GET requests → write JSON files

**TypeScript equivalent (per-collector Lambda — no NestJS DI, no routing):**
```typescript
// lambdas/municipal-collectors/collectors/legistar.collector.ts
import { S3Client, PutObjectCommand } from '@aws-sdk/client-s3';

const s3 = new S3Client({});

export async function collectLegistar(
  slug: string,
  client: string, // e.g., "charlottenc", "columbus", "cityofdallas"
  bucket: string,
  collectionType: 'full' | 'agenda_sync',
): Promise<CollectorResult> {
  const baseUrl = `https://webapi.legistar.com/v1/${client}`;

  // Collect bodies, events, matters (paginated)
  const bodies = await fetchPaginated(`${baseUrl}/Bodies`);
  const events = await fetchPaginated(`${baseUrl}/Events`, {
    $filter: `EventDate ge datetime'${sinceDate}'`,
  });
  const matters = await fetchPaginated(`${baseUrl}/Matters`, {
    $filter: `MatterLastModifiedUtc ge datetime'${sinceDate}'`,
  });

  // Upload to S3 (dated folder per collection run)
  const datePrefix = new Date().toISOString().split('T')[0]; // e.g., "2026-03-09"
  const s3Prefix = `${slug}/legislative/${datePrefix}`;
  await putJson(bucket, `${s3Prefix}/bodies.json`, bodies);
  await putJson(bucket, `${s3Prefix}/events.json`, events);
  await putJson(bucket, `${s3Prefix}/matters.json`, matters);

  // Download PDFs (matter attachments)
  for (const matter of matters) {
    const attachments = await fetchAttachments(baseUrl, matter.MatterId);
    for (const att of attachments) {
      const pdf = await fetch(att.MatterAttachmentHyperlink);
      const body = Buffer.from(await pdf.arrayBuffer());
      await s3.send(new PutObjectCommand({
        Bucket: bucket, Key: `${s3Prefix}/pdfs/${att.MatterAttachmentId}.pdf`, Body: body,
      }));
    }
  }

  // Write manifest
  await putJson(bucket, `${s3Prefix}/_manifest.json`, {
    citySlug: slug, category: 'legislative', collectionType, collector: 'municipal-legistar',
    collectedAt: new Date().toISOString(), lastSyncedAt: null,
    files: {
      'bodies.json': { records: bodies.length },
      'events.json': { records: events.length },
      'matters.json': { records: matters.length },
    },
    pdfs: /* pdf count */,
  });

  return { s3Prefix, matters: matters.length, events: events.length, bodies: bodies.length };
}

async function fetchPaginated(url: string, params?: Record<string, string>): Promise<any[]> {
  const results = [];
  let skip = 0;
  const top = 1000;
  while (true) {
    const resp = await fetch(`${url}?$top=${top}&$skip=${skip}&${new URLSearchParams(params)}`);
    const data = await resp.json();
    if (!data.length) break;
    results.push(...data);
    skip += top;
    await new Promise(r => setTimeout(r, 250)); // Rate limit: 0.25s
  }
  return results;
}

// handlers/legistar.handler.ts (per-collector entry point — no routing needed)
import { collectLegistar } from '../collectors/legistar.collector';
import { sendCompletion } from '../shared/completion';

export async function handler(event: { citySlug: string; collectionType: string; config: any }) {
  const bucket = process.env.S3_BUCKET!;
  const result = await collectLegistar(event.citySlug, event.config.legistarClient, bucket, event.collectionType);
  await sendCompletion('MUNICIPAL_LEGISLATIVE_COMPLETE', event.citySlug, {
    s3Prefix: result.s3Prefix,
    collectionType: event.collectionType,
    fileCount: Object.keys(result).length,
    recordCounts: { matters: result.matters, events: result.events, bodies: result.bodies },
  });
}

// Fan-out Lambda invokes this directly:
// await lambda.invoke({ FunctionName: 'municipal-legistar', InvocationType: 'Event', Payload: JSON.stringify({ citySlug, collectionType, config }) })
```

### BoardDocs (HTML parsing port)

**Python:** `HTMLParser` subclasses → parse AJAX POST responses

**TypeScript:** Use `cheerio` for HTML parsing:
```typescript
import * as cheerio from 'cheerio';

// Python: class CommitteeParser(HTMLParser) → handle_starttag, handle_data
// TypeScript: cheerio selectors
const $ = cheerio.load(htmlResponse);
const committees = $('a[onclick]').map((_, el) => ({
  id: $(el).attr('onclick')?.match(/load_category\('(.+?)'\)/)?.[1],
  name: $(el).text().trim(),
})).get();
```

### Budget Collectors (3 types)

**NC — Opendatasoft (LINC):**
```typescript
// Port of budget_linc.py — works for Charlotte + Raleigh
const baseUrl = 'https://linc.osbm.nc.gov/api/explore/v2.1/catalog/datasets';
const records = await this.fetchPaginated(
  `${baseUrl}/government/records`,
  { where: `area_name='${municipalityName}'`, limit: '100' }
);
```

**TX — Socrata (data.texas.gov + city portals):**
```typescript
// New — SoQL query similar to Opendatasoft
const records = await this.fetchSocrata(
  'https://data.texas.gov/resource/dyv5-3bjd.json',
  { $where: `governmentname='${cityName}' AND governmenttype='CITY'`, $order: 'fiscalyear DESC' }
);
```

**OH — Auditor Spreadsheets:**
```typescript
// New — download Excel, parse to JSON
const xlsx = await this.downloadFile('https://ohioauditor.gov/references/...');
const rows = this.parseExcel(xlsx, { filterCity: cityName });
```

### Constituent Data (people-api + Databricks hybrid)

**POC approach:** Haystaq collector queries Databricks for demographics AND issue scores.

**Production approach:** Split into two sources:

**Demographics — from people-api (already exists):**
```typescript
// constituentData.service.ts
// Use the elections service to resolve which districts cover this city,
// then call people-api for pre-computed stats.
const district = await this.electionsService.getDistrictId(state, l2Type, l2Name);
const stats = await this.peopleApiClient.getDistrictStats(district.id);
// stats.buckets → { age: [...], income: [...], education: [...] }
// stats.totalConstituents → 642,000
```

**Issue scores — from Databricks REST API (new query):**
```typescript
// haystaqIssueScores.collector.ts
// Only query the haystaq_dna_scores tables — skip demographics since people-api has them.
const response = await fetch(
  `https://${hostname}/api/2.0/sql/statements`,
  {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify({
      warehouse_id: warehouseId,
      statement: `SELECT ROUND(AVG(CAST(s.hs_affordable_housing_gov_has_role AS DOUBLE)), 1), ...
                  FROM ${uniformTable} u JOIN ${scoresTable} s ON u.LALVOTERID = s.LALVOTERID
                  WHERE UPPER(u.${filterColumn}) = '${filterValue}'`,
      wait_timeout: '30s',
    }),
  }
);
// Returns average issue scores — no pandas needed, REST API gives structured JSON.
```

This avoids duplicating the voter demographics pipeline that people-api already maintains.

## Queue Integration

### gp-api SQS (completion messages only)

Add to `src/queue/queue.types.ts`:

```typescript
export enum QueueType {
  // ... existing types ...
  MUNICIPAL_LEGISLATIVE_COMPLETE = 'municipalLegislativeComplete',
  MUNICIPAL_BUDGET_COMPLETE = 'municipalBudgetComplete',
  MUNICIPAL_ANALYZE_COMPLETE = 'municipalAnalyzeComplete',        // Phase 5+
  MUNICIPAL_PDF_EXTRACTION_COMPLETE = 'municipalPdfExtractionComplete', // Phase 5+
}
```

Add handler cases in `queueConsumer.service.ts` (follow existing `POLL_ANALYSIS_COMPLETE` pattern). Each collector sends its own completion type — legislative and budget arrive independently. Handlers only update Prisma records — the heavy work already happened in Lambda.

### No Intermediate SQS Queue

Unlike the previous design, there is no SQS queue between the fan-out and collector Lambdas. The fan-out invokes collector Lambdas directly using async `lambda.invoke()`. This eliminates 6+ SQS queues.

**Retry/DLQ:** Each collector Lambda has an `EventInvokeConfig` with `MaximumRetryAttempts: 2` and an `OnFailure` destination (SQS DLQ). Failed invocations land in a shared `municipal-collector-dlq` for inspection.

**API trigger:** `POST /configs/:slug/collect` in gp-api invokes the collector Lambdas directly (same `lambda.invoke()` pattern as fan-out).

## LLM Analysis Port

**Python (04_run_analysis.py):** Uses `google-genai` + Pydantic for structured output

**TypeScript:** Use `@google/genai` SDK + Zod:

```typescript
import { GoogleGenAI } from '@google/genai';
import { z } from 'zod';

// Python Pydantic model:
// class TopicArea(BaseModel):
//     topic_name: str
//     matter_count: int
//     key_matters: list[str]

// TypeScript Zod schema:
const TopicArea = z.object({
  topic_name: z.string(),
  matter_count: z.number(),
  key_matters: z.array(z.string()),
});

const LegislativeOverview = z.object({
  total_matters: z.number(),
  topic_areas: z.array(TopicArea),
});

// Call Gemini with structured output
const ai = new GoogleGenAI({ apiKey: process.env.GEMINI_API_KEY });
const response = await ai.models.generateContent({
  model: 'gemini-2.5-flash',
  contents: prompt,
  config: {
    responseMimeType: 'application/json',
    responseSchema: zodToGeminiSchema(LegislativeOverview), // Helper to convert Zod → Gemini schema
  },
});
const parsed = LegislativeOverview.parse(JSON.parse(response.text));
```

## PDF Extraction Sidecar (Python, in gp-ai-projects)

### What It Does

A small Fargate task that:
1. Reads PDFs from `s3://municipal-data-{env}/{slug}/legislative/{date}/pdfs/`
2. Extracts text (PyMuPDF) + tables (pdfplumber)
3. Writes extracted JSON to `s3://municipal-data-{env}/{slug}/extracted/{date}/`
4. Sends `MUNICIPAL_PDF_EXTRACTION_COMPLETE` SQS message to gp-api's queue (includes `s3Prefix`)

### Terraform Module

Follow the existing `infrastructure/modules/serve-analyze-fargate/` pattern:

```
infrastructure/modules/municipal-pdf-extractor/
  main.tf              # ECS task definition, Lambda trigger, Step Function
  variables.tf
  outputs.tf
```

Key differences from serve-analyze:
- Lighter resources (2 vCPU, 8GB RAM — PDF extraction is CPU-bound)
- S3 trigger watches `municipal-data-{env}/*/raw/pdfs/` prefix
- Container runs a thin CLI wrapper around existing `pdf_extractor.py`
- On completion, sends SQS message to gp-api (not SNS)

### CLI Wrapper

```python
# briefing_poc/scripts/extract_pdfs_s3.py
"""Read PDFs from S3, extract, write back to S3, notify gp-api via SQS."""

import boto3, json, sys
from collectors.pdf_extractor import extract_pdf

s3 = boto3.client('s3')
sqs = boto3.client('sqs')

def main(bucket: str, city_slug: str, queue_url: str):
    # List PDFs in S3
    pdfs = s3.list_objects_v2(Bucket=bucket, Prefix=f"{city_slug}/raw/pdfs/")

    for obj in pdfs.get('Contents', []):
        key = obj['Key']
        # Download PDF to /tmp
        local_path = f"/tmp/{key.split('/')[-1]}"
        s3.download_file(bucket, key, local_path)

        # Extract (reuses existing pdf_extractor.py)
        result = extract_pdf(local_path)

        # Upload extracted JSON
        out_key = key.replace('/raw/pdfs/', '/raw/extracted/').replace('.pdf', '.json')
        s3.put_object(Bucket=bucket, Key=out_key, Body=json.dumps(result))

    # Notify gp-api
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            'type': 'municipalPdfExtractionComplete',
            'citySlug': city_slug,
            's3Prefix': f"{city_slug}/raw/extracted/",
        }),
        MessageGroupId='municipal-data',
    )
```

## API Endpoints

```
POST   /v1/municipal-data/municipalities                    # Register a city
GET    /v1/municipal-data/municipalities                    # List cities
GET    /v1/municipal-data/municipalities/:slug              # Get city + latest data summary
POST   /v1/municipal-data/municipalities/:slug/collect      # Trigger pipeline run
GET    /v1/municipal-data/municipalities/:slug/runs         # List pipeline runs
GET    /v1/municipal-data/municipalities/:slug/data/:type   # Get specific analysis JSON
```

Protected by `@Roles(UserRole.admin)` initially.

## How Product Features Access City Data

Once the pipeline runs, any gp-api service can query city data through Prisma:

```typescript
// Get the legislative overview for Charlotte
const snapshot = await prisma.municipalDataSnapshot.findUnique({
  where: {
    configId_dataType: {
      configId: config.id,
      dataType: 'legislative_overview',
    },
  },
});
// snapshot.data → { total_matters: 686, topic_areas: [...] }

// Get all analysis data for a city
const allData = await prisma.municipalDataSnapshot.findMany({
  where: { configId: config.id },
});

// Get city demographics from election-api Place (not duplicated locally)
const place = await this.electionsService.getPlace(config.placeId);
// place → { name: "Charlotte", population: 879709, incomeHouseholdMedian: 62817, ... }

// Get voter district stats from people-api (not duplicated locally)
const districtStats = await this.peopleApiClient.getDistrictStats(districtId);
// districtStats → { totalConstituents: 642000, buckets: { age: [...], income: [...] } }
```

For features that need raw data (e.g., browsing individual matters), look up the S3 prefix from PipelineRun, then read from S3:

```typescript
// 1. Get the latest legislative run for Charlotte
const latestRun = await prisma.municipalPipelineRun.findFirst({
  where: { configId: config.id, status: 'complete', s3Prefix: { startsWith: `${config.slug}/legislative/` } },
  orderBy: { completedAt: 'desc' },
});
// latestRun.s3Prefix → "charlotte-nc/legislative/2026-03-09/"

// 2. Read the manifest to see what's available
const manifest = await this.s3.getFile('municipal-data-prod', `${latestRun.s3Prefix}_manifest.json`);
// manifest → { files: { "matters.json": { records: 12450 }, ... }, collectedAt: "2026-03-09T06:02:14Z" }

// 3. Read the specific file you need
const matters = await this.s3.getFile('municipal-data-prod', `${latestRun.s3Prefix}matters.json`);
```

---

## Implementation Sequence

### Phase 1: Foundation (2-3 days)
1. Create Prisma models (`MunicipalDataConfig`, `MunicipalPipelineRun`, `MunicipalDataSnapshot`) + migration
2. Create `municipalData` NestJS module with CRUD service and controller
3. Add `QueueType` enum values (`MUNICIPAL_COLLECT_COMPLETE`, etc.)
4. Add `municipal-data-{env}` S3 bucket to Pulumi deploy
5. Port `city_config.json` schema to Zod (must support all systems: Legistar, BoardDocs, PrimeGov, Opendatasoft, Socrata, OH Auditor)
6. Seed all 7 city configs — link to existing election-api Place UUIDs via `placeId`

### Phase 2: Lambda Infrastructure (1-2 days)
7. Create `lambdas/municipal-collectors/` with per-collector handlers + shared utilities (S3, HTTP, config, completion)
8. Create `lambdas/municipal-fanout/` — reads active configs, invokes collector Lambdas per city
9. Add Pulumi resources to `deploy/index.ts` (loop over collector definitions):
   - Per-collector Lambdas: `municipal-legistar`, `municipal-boarddocs`, `municipal-primegov`, `municipal-budget-opendatasoft`, `municipal-budget-socrata`, `municipal-budget-ohio-auditor`
   - Fan-out Lambda (Node 22, 256MB, 30s timeout)
   - EventBridge rules: daily `cron(0 6 * * ? *)` + weekly `cron(0 2 ? * SUN *)`
   - Shared DLQ for failed collector invocations
   - Each collector Lambda has `EventInvokeConfig` (max 2 retries, OnFailure → DLQ)
10. Test: fan-out Lambda → invokes legistar Lambda → writes to S3 (with a stub collector)

### Phase 3: Legislative Collectors (2-3 days)
11. Port Legistar collector — covers Charlotte, Columbus, Cleveland, Dallas, Austin, San Antonio (historical)
12. Port BoardDocs collector — Raleigh (HTML parsing via cheerio, undocumented POST API)
13. Build PrimeGov collector — San Antonio (JSON API for meetings, HTML for agenda items)
14. Each collector has its own handler — no routing needed. Handler calls collector, sends typed completion message.

### Phase 4: Budget Collectors (1-2 days)
15. Port NC Budget (Opendatasoft/LINC) — Charlotte + Raleigh
16. Build TX Budget (Socrata) — Dallas, Austin, San Antonio (statewide debt/tax + city portals)
17. Build OH Budget (Spreadsheet parser) — Columbus, Cleveland (download from OH Auditor, parse Excel)

### Phase 5: API + Completion Handling (1-2 days)
18. Build REST API endpoints (config CRUD, trigger collection, list runs, get data)
19. Wire SQS completion handler in gp-api — handles `MUNICIPAL_LEGISLATIVE_COMPLETE` and `MUNICIPAL_BUDGET_COMPLETE` independently
20. API trigger: `POST /configs/:slug/collect` invokes collector Lambdas directly (same pattern as fan-out)
21. Test end-to-end: EventBridge → fan-out → per-collector Lambdas → S3 → gp-api completion

### Phase 6: Analysis Pipeline (3-4 days, when ready)
22. Port Pydantic schemas to Zod (all 6 passes)
23. Port 6-pass LLM analysis to TypeScript Lambda using `@google/genai`
24. Port topic mapping, mismatch analysis, quick wins
25. Wire `MUNICIPAL_ANALYZE_COMPLETE` handler to save results to PostgreSQL + S3
26. Verify output matches Python POC for Charlotte (regression test)

### Phase 7: PDF Sidecar + Constituent Data (2-3 days, when ready)
27. Create Terraform module in gp-ai-projects (based on serve-analyze-fargate)
28. Write Python CLI wrapper for S3-based PDF extraction
29. Build Haystaq issue scores Lambda — Databricks REST for issue scores (demographics from people-api)
30. Add Slack notifications for pipeline failures
31. Document onboarding process for new cities

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| PDF extraction as cross-repo dependency | Pipeline stalls if gp-ai-projects deploy is blocked | Run analysis passes 1-3, 5-6 without pass 4 (document summaries). Pass 4 catches up later. |
| LLM costs at scale (3,200 cities) | ~$128-192 per full run, ~$500-800/month weekly | Incremental analysis — only re-analyze cities whose raw data changed. |
| Lambda cold starts | First invocation slower (~1-2s) | Minimal impact — collectors are batch jobs, not user-facing. Use provisioned concurrency only if needed. |
| Lambda 15-min timeout | Large city could exceed timeout during full collection | Legislative and budget already run as separate Lambdas. Per-city legislative fits in 1-3 min. |
| Collector reliability | API rate limits, connection drops, data format changes | Per-collector rate limiting, retry with backoff, DLQ catches persistent failures per city. |
| Gemini API changes | `@google/genai` SDK is relatively new | Pin SDK version, abstract behind a service class for easy swapping. |

---

## Key Reference Files

### gp-api (patterns to follow)
| File | What to Reference |
|------|------------------|
| `src/queue/queue.types.ts` | Add new QueueType values here |
| `src/queue/consumer/queueConsumer.service.ts` | Add handler cases (follow POLL_ANALYSIS_COMPLETE pattern) |
| `src/vendors/aws/services/s3.service.ts` | Use for all S3 operations |
| `src/organizations/services/organizations.service.ts` | Pattern for resolving placeId, positionId, districtId from election-api |
| `src/elections/services/elections.service.ts` | Existing service for calling election-api (getDistrictId, getPositionById, etc.) |
| `deploy/index.ts` | Add S3 bucket, SQS queue, Lambda functions, EventBridge rules |

### election-api (existing models to reference)
| File | What It Contains |
|------|-----------------|
| `prisma/schema/place.prisma` | Place model — geographic entities with demographics. Link via `placeId`. |
| `prisma/schema/district.prisma` | District model — L2 voter districts (state + L2DistrictType + L2DistrictName) |
| `prisma/schema/position.prisma` | Position model — BallotReady offices linked to districts |
| `prisma/schema/race.prisma` | Race model — electoral races linked to places |

### people-api (existing models to query)
| File | What It Contains |
|------|-----------------|
| `prisma/schema/District.prisma` | District model — mirrors election-api UUIDs, links to voters |
| `prisma/schema/DistrictStats.prisma` | Pre-computed demographic buckets (age, income, education, etc.) per district |
| `prisma/schema/DistrictVoter.prisma` | Junction table — which voters are in which districts |

### gp-ai-projects (source code to port)
| File | What It Contains |
|------|-----------------|
| `briefing_poc/collectors/legistar.py` | Primary collector — pure HTTP with pagination |
| `briefing_poc/collectors/boarddocs.py` | HTML parser classes → port to cheerio |
| `briefing_poc/collectors/escribemeetings.py` | JSON API + HTML parsing |
| `briefing_poc/collectors/budget_linc.py` | Opendatasoft API pagination |
| `briefing_poc/collectors/haystaq_voter.py` | Issue score queries only — demographics come from people-api |
| `briefing_poc/collectors/pdf_extractor.py` | Keep in Python — wrap for S3 |
| `briefing_poc/scripts/04_run_analysis.py` | 6 LLM passes + Pydantic schemas → port to Zod |
| `briefing_poc/scripts/07_council_vs_constituent.py` | Pure math — trivial port |
| `briefing_poc/scripts/09_generate_quick_wins.py` | Pure data joining — trivial port |
| `briefing_poc/scripts/city_config.py` | Config class → port to Zod schema |
| `briefing_poc/cities/charlotte/city_config.json` | Reference config structure |

### gp-ai-projects (infrastructure pattern)
| File | What to Reference |
|------|------------------|
| `infrastructure/modules/serve-analyze-fargate/main.tf` | Template for PDF extractor Fargate module |
| `infrastructure/modules/serve-analyze-fargate/step-function-definition.json` | Step Function structure to reuse |
