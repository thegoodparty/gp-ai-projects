# Municipal Data Pipeline — Technical Plan

## What We're Building

A production pipeline that collects municipal government data (legislative records, budgets, meeting agendas) for multiple cities and makes it available to product features via gp-api. The briefing POC proved this works for 3 NC cities. We're now scaling to 7+ cities across 3 states.

### Target Cities

| City | State | Legislative Source | Budget Source |
|------|-------|--------------------|---------------|
| Charlotte | NC | Legistar | NC LINC |
| Raleigh | NC | BoardDocs | NC LINC |
| Columbus | OH | Legistar | OH Auditor |
| Cleveland | OH | Legistar | OH Auditor |
| Dallas | TX | Legistar | TX data.texas.gov |
| Austin | TX | Legistar | TX data.texas.gov |
| San Antonio | TX | PrimeGov | TX data.texas.gov |

Architecture supports adding new cities via config — no code changes required.

---

## What Data We Collect

For each city, we collect two categories of data:

**Legislative data** — from city council systems (Legistar, BoardDocs, PrimeGov):
- Council bodies and committees
- Meeting events and agendas
- Legislative matters (ordinances, resolutions, appointments)
- Roll-call votes
- PDF attachments (staff reports, resolutions)

**Budget/fiscal data** — from state-level open data portals:
- Revenue and expenditure history (up to 44 years for NC)
- Property tax rates
- Debt and bond data (TX)

---

## How It Works

### The Pipeline Flow

```
1. TRIGGER
   EventBridge cron fires on schedule (or an API call triggers manually)
        │
        ▼
2. FAN-OUT
   A fan-out Lambda reads active city configs from the database.
   For each city, it invokes the correct collector Lambdas — async, in parallel.
   7 cities × 2 collectors = up to 14 Lambdas running simultaneously.
        │
        ▼
3. COLLECT (all cities in parallel)
   Each collector Lambda handles one city:
     • Calls the city's public API (Legistar, BoardDocs, etc.)
     • Writes raw JSON files to a dated S3 folder
     • Writes a _manifest.json describing the folder contents
     • Sends a completion message to gp-api via SQS

   Legislative and budget collectors run independently —
   product features can show legislative data as soon as it arrives,
   without waiting for budget.
        │
        ▼
4. TRACK
   gp-api's SQS consumer receives each completion message:
     • Creates a PipelineRun record in PostgreSQL
     • Stores the S3 path so we know where the data lives
     • Updates the city's lastCollectedAt timestamp
        │
        ▼
5. SERVE
   Any product feature can now access the data:
     1. Query Prisma for the latest PipelineRun → get S3 path
     2. Read _manifest.json → know what files exist
     3. Read the specific file needed
```

### Scheduling

| Schedule | What Happens | Purpose |
|----------|-------------|---------|
| **Daily 6 AM UTC** | Agenda sync — checks for newly published meeting agendas in the next 14 days | Agendas are published 1-6 days before meetings. Daily sync catches them. |
| **Weekly Sunday 2 AM UTC** | Full collection — pulls all data for every city from scratch | Creates a fresh, complete snapshot. Catches anything the daily sync missed. |

Both schedules trigger the same fan-out → collector flow. The difference is what the collectors query — daily sync only looks at upcoming events, weekly does everything.

### Manual Trigger

`POST /v1/municipal-data/configs/:slug/collect` triggers collection for a specific city on demand.

---

## Where It Lives

### The Split: TypeScript + Python Sidecar

Almost everything is TypeScript in or alongside gp-api. The one exception is PDF extraction, which requires Python-only libraries.

| Component | Repo | Language | Why |
|-----------|------|----------|-----|
| All collectors (Legistar, BoardDocs, budgets) | gp-api (Lambda functions) | TypeScript | Pure HTTP — no Python dependencies needed |
| Fan-out + scheduling | gp-api (Lambda + EventBridge) | TypeScript | Reads city configs from our DB, invokes collectors |
| Completion handling | gp-api | TypeScript | SQS consumer — same pattern as poll analysis |
| API endpoints | gp-api | TypeScript | Config CRUD, trigger collection, serve data |
| LLM analysis (6 passes) | gp-api (Lambda) | TypeScript | Google's `@google/genai` SDK has a JS version |
| PDF text extraction | gp-ai-projects (Fargate) | Python | pdfplumber has no JS equivalent for table extraction |

**Why not all in gp-ai-projects?** The consumers of city data are product features in gp-api. Keeping the pipeline in gp-api means data flows directly into PostgreSQL/Prisma where those features already live — no cross-repo API calls for every read.

### Per-Collector Lambdas

Each collector type is its own Lambda function:

- `municipal-legistar` — handles Charlotte, Columbus, Cleveland, Dallas, Austin
- `municipal-boarddocs` — handles Raleigh
- `municipal-primegov` — handles San Antonio
- `municipal-budget-opendatasoft` — handles NC cities (Charlotte, Raleigh)
- `municipal-budget-socrata` — handles TX cities (Dallas, Austin, San Antonio)
- `municipal-budget-ohio-auditor` — handles OH cities (Columbus, Cleveland)
- `municipal-fanout` — the router that invokes the others

**Why separate Lambdas instead of one big collector?**
- Each city runs in its own Lambda invocation — full parallelism, independent retries
- A crash collecting Dallas doesn't affect Charlotte
- Dependencies are isolated (cheerio only in BoardDocs, xlsx only in OH Budget)
- Adding a new collector type = adding one new Lambda. Nothing else changes.

---

## Data Storage

### S3: Organized by City, Category, and Date

```
municipal-data-{env}/
  charlotte-nc/
    legislative/
      2026-03-02/                ← weekly snapshot (immutable)
        _manifest.json           ← table of contents for this folder
        bodies.json
        events.json
        matters.json
        votes.json
        persons.json
        pdfs/
      2026-03-09/                ← next weekly snapshot
        _manifest.json
        ...
    budget/
      2026-03-02/
        _manifest.json
        government_fiscal.json
        property_tax_rate.json
```

**Key rules:**
- Each weekly full collection creates a **new dated folder** — previous weeks are immutable snapshots
- Daily syncs **update files in the most recent folder** (e.g., add newly published agendas)
- Old folders expire after 90 days via S3 lifecycle policy (~12 weekly snapshots retained)

### Manifest Files

Every collection folder has a `_manifest.json` that describes its contents:

```json
{
  "citySlug": "charlotte-nc",
  "category": "legislative",
  "collectionType": "full",
  "collectedAt": "2026-03-09T06:02:14Z",
  "collector": "municipal-legistar",
  "files": {
    "bodies.json": { "records": 21 },
    "events.json": { "records": 847, "dateRange": ["2020-01-06", "2026-10-15"] },
    "matters.json": { "records": 12450 },
    "votes.json": { "records": 3200 }
  },
  "pdfs": 128
}
```

This means any consumer can check what's in a folder without opening every file.

### PostgreSQL: Tracking and Derived Data

| What | Where | Purpose |
|------|-------|---------|
| City pipeline configs | `MunicipalDataConfig` | Which cities are active, what collectors to use, API keys |
| Collection run tracking | `MunicipalPipelineRun` | Status of each run, S3 path to the data, record counts |
| Analysis results | `MunicipalDataSnapshot` (JSONB) | LLM-derived insights, directly queryable by product features |

The `PipelineRun` stores the S3 prefix for each collection. Finding data is: query Prisma for the latest run → read from S3. No S3 listing needed.

---

## What the Data Enables (Pipeline Stages)

### Stage 1: Raw Collection (what we're building first)

Collectors pull raw data from public APIs. Some of this is immediately usable:

| Data | Directly Usable? |
|------|-----------------|
| Vote records (roll-call votes, member names) | Yes — can show voting history right away |
| Budget numbers (revenue, expenditure, tax rates) | Yes — can chart trends right away |
| Meeting agendas and events | Yes — can list upcoming meetings |
| Legislative matters (titles, statuses) | Partially — can list/search, but no categorization |
| PDF text (staff reports) | No — unstructured, needs LLM analysis |

### Stage 2: LLM Analysis (built after ingestion is working)

Six Gemini passes turn raw data into structured JSON that directly powers UI components:

| Pass | What It Produces |
|------|-----------------|
| Legislative Overview | Topic breakdown — what the council spends time on |
| Vote Analysis | Voting patterns — unanimous vs. contested, frequent movers |
| Budget Analysis | Financial trends — revenue, expenditure, fiscal pressures |
| Document Summaries | Top 50 staff reports with key issues and fiscal impact |
| Committee Analysis | Committee profiles, workload, composition |
| Synthesis | Executive summary, key themes, priorities |

**Cost:** ~$0.04-0.06 per city for all 6 passes (Gemini 2.5 Flash).

### Stage 3: Data Joining (the differentiator)

Cross-references council activity with constituent priorities:

- **Council vs. Constituent mismatch** — Where is the council spending time on things voters don't care about? Where are voter priorities being ignored?
- **Quick wins** — Concrete actions tied to gap areas with specific matter references

---

## Infrastructure

### New AWS Resources (via Pulumi)

| Resource | Purpose |
|----------|---------|
| S3 bucket `municipal-data-{env}` | Raw data + analysis output |
| 6 collector Lambdas | One per collector type (Legistar, BoardDocs, PrimeGov, 3 budget types) |
| 1 fan-out Lambda | Reads city configs, invokes collectors |
| 2 EventBridge rules | Daily agenda sync + weekly full collection |
| 1 shared DLQ | Catches failed collector invocations for inspection |

### New gp-api Components

| Component | Purpose |
|-----------|---------|
| `municipalData` NestJS module | Config CRUD, pipeline tracking, API endpoints |
| 3 Prisma models | `MunicipalDataConfig`, `MunicipalPipelineRun`, `MunicipalDataSnapshot` |
| 2 new SQS queue types | `MUNICIPAL_LEGISLATIVE_COMPLETE`, `MUNICIPAL_BUDGET_COMPLETE` |

### Existing Infrastructure We're Reusing

| What | Where | How We Use It |
|------|-------|---------------|
| SQS completion pattern | gp-api (`POLL_ANALYSIS_COMPLETE`) | Same pattern for collector completion messages |
| S3Service | gp-api | Reading data in API endpoints |
| Pulumi deploy pipeline | gp-api `deploy/index.ts` | Adding new Lambda + EventBridge resources |
| DistrictStats | people-api | Voter demographics — already pre-computed, no new work |
| Place model | election-api | City identity — we reference it, don't duplicate it |

---

## Implementation Phases

### Phase 1: Foundation
- Prisma models + migration
- NestJS module skeleton
- S3 bucket + Lambda infrastructure (Pulumi)
- City config schema + seed 7 cities

### Phase 2: Legislative Collectors
- Legistar collector (covers 5-6 cities)
- BoardDocs collector (Raleigh)
- PrimeGov collector (San Antonio, if keeping)

### Phase 3: Budget Collectors
- NC Opendatasoft (Charlotte, Raleigh)
- TX Socrata (Dallas, Austin, San Antonio)
- OH Spreadsheet parser (Columbus, Cleveland)

### Phase 4: API + End-to-End
- REST endpoints (config CRUD, trigger collection, serve data)
- SQS completion handler
- EventBridge scheduling
- End-to-end validation for all 7 cities

### Phase 5: Analysis Pipeline
- Port 6 LLM analysis passes from Python to TypeScript
- Port data joining (mismatch, quick wins)
- Save results to PostgreSQL JSONB

### Phase 6: PDF Extraction + Constituent Data
- Python Fargate sidecar in gp-ai-projects for PDF extraction
- Haystaq issue scores via Databricks REST API

Phases 1-4 deliver data ingestion for all 7 cities. Phases 5-6 add analysis and are built once ingestion is stable.

---

## Open Decisions

| Decision | Options | Status |
|----------|---------|--------|
| San Antonio | Keep with PrimeGov collector, or swap for Fort Worth/El Paso (both on Legistar) | Needs PM decision |
| 3 additional cities | Architecture supports 10+, need to pick the next 3 | Needs PM input |

---

## Key Risks

| Risk | Mitigation |
|------|------------|
| BoardDocs API is undocumented | POST-based endpoints confirmed working. Save response samples for regression testing. |
| PrimeGov requires HTML scraping for agenda detail | Can swap San Antonio for a Legistar city if too complex. |
| OH budget data is spreadsheet-only (no API) | Download + parse Excel. Pin to known URL format. |
| Lambda 15-min timeout for large cities | Legislative and budget already run as separate Lambdas. Per-city fits in 1-3 min. |
| LLM costs at scale (thousands of cities) | Only re-analyze cities whose raw data changed. ~$0.05/city is negligible at 7-10 cities. |
