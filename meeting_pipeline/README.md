# meeting_pipeline

Collects city council meeting agendas for pilot officials, extracts agenda items via LLM, and generates personalized briefings.

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) package manager
- AWS CLI with SSO access to the `goodparty` account
- A Gemini API key

## Setup

### 1. Install dependencies

```bash
cd meeting_pipeline
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Required for LLM extraction and briefing generation |
| `TAVILY_API_KEY` | Required for source discovery (`source_discover.py`) |
| `DATABRICKS_SERVER_HOSTNAME` | Required for `collect_haystaq_batch.py` (constituent voter data) |
| `DATABRICKS_HTTP_PATH` | Required for `collect_haystaq_batch.py` |
| `DATABRICKS_API_KEY` | Required for `collect_haystaq_batch.py` |
| `STORAGE_BACKEND` | `s3` (recommended) or `local` |
| `S3_BUCKET` | S3 bucket name — use `meeting-pipeline-dev` for dev |
| `AWS_PROFILE` | AWS SSO profile name — use `goodparty` |

### 3. Log in to AWS SSO

All pipeline data (source configs, collected data, PDFs, normalized JSON, briefings) lives in S3.
You need an active AWS SSO session to run any pipeline script.

```bash
aws sso login --profile goodparty
```

Your session will stay active for several hours. Re-run this command if you get auth errors.

To verify access:
```bash
aws s3 ls s3://meeting-pipeline-dev/ --profile goodparty
```

---

## Full Pipeline (in order)

All scripts must be run from the `meeting_pipeline/` directory:

```bash
cd meeting_pipeline
```

### 1. Discover sources

Find the best agenda URL for a city and write `sources/{city}/source.json` to S3.

```bash
uv run python scripts/source_discover.py --city "Durham" --state NC
```

### 2. Collect meeting data

Collect all pilot cities in one command. Reads `source.json` per city from S3, routes to the right collector automatically.

```bash
uv run python scripts/collect_pilot_batch.py
uv run python scripts/collect_pilot_batch.py --city "Durham NC"  # single city
uv run python scripts/collect_pilot_batch.py --no-pdfs           # metadata only
```

Output (written to S3): `sources/{city}/data/{platform}/meetings.json` + `pdfs/`

### 3. Collect Haystaq voter data

```bash
uv run python scripts/collect_haystaq_batch.py
```

Output (written to S3): `sources/{city}/constituent/issue_scores.json`

### 4. Build extraction queue

Matches officials (from `pilot_registry.py`) to their collected meeting data and checks agenda status.

```bash
uv run python scripts/generate_meeting_queue.py
```

Output (written to S3): `output/meeting_queue.json`

### 5. Extract and normalize

Reads PDFs from S3, extracts text, sends to Gemini for structured extraction, writes normalized JSON back to S3.
Already-normalized meetings are skipped automatically (use `--force` to re-run).

```bash
uv run python scripts/extract_and_normalize.py
uv run python scripts/extract_and_normalize.py --force    # re-run all
uv run python scripts/extract_and_normalize.py --dry-run  # preview only
```

Output (written to S3): `output/normalized/{city}_{date}.json`

### 6. Generate briefings

3-pass Gemini pipeline: categorize → cards → detail pages. Uses Haystaq data if available.

```bash
uv run python scripts/generate_briefing.py --batch
uv run python scripts/generate_briefing.py --city johnstown-OH
```

Output (written to S3): `output/briefings/{city}_{date}_briefing.json`

---

## Directory Structure

```
meeting_pipeline/
├── collectors/          # Platform collectors (one per platform)
├── collection_agent/    # Routing agent — dispatches cities to the right collector
├── scripts/             # Pipeline scripts (run these in order above)
├── prompts/             # LLM prompts for extraction and briefing generation
├── tests/               # Test suite
├── unused_collectors/   # Experimental collectors (not in production use)
├── unused_scripts/      # Retired scripts
└── docs/                # Documentation
```

> `sources/` and `output/` are not in the repo — all pipeline data lives in S3 (`meeting-pipeline-dev`).

## Collectors

| Module | Platform | Notes |
|--------|----------|-------|
| `civicclerk.py` | CivicClerk OData API | Most pilot cities |
| `civicplus_scraper.py` | CivicPlus AgendaCenter | AJAX-based, auto-discovers categories |
| `granicus_scraper.py` | Granicus / Swagit | Classic RSS + New Swagit JSON |
| `legistar.py` | Legistar REST API | |
| `escribemeetings.py` | eSCRIBE | POST-based JSON API |
| `boarddocs.py` | BoardDocs | AJAX scraper |
| `generic_html_scraper.py` | Custom city HTML | Multi-strategy, Tier 3 cities |

## Pilot Registry

`pilot_registry.py` is the single source of truth for which officials are in the pilot.
All scripts (`collect_pilot_batch.py`, `generate_meeting_queue.py`, `collect_haystaq_batch.py`) read from it.

To add or remove an official, edit `PILOT_OFFICIALS` in `pilot_registry.py`.

## Collection Agent

`collection_agent/` routes any city to the right collector based on `source.json`, with a fallback
chain (dedicated collector → replay → Playwright+LLM) for unknown platforms.

`collect_pilot_batch.py` uses this internally — you don't need to call the agent directly.
For one-off collection or debugging:

```bash
uv run python -m meeting_pipeline.collection_agent.run --city "Durham NC"
uv run python -m meeting_pipeline.collection_agent.run --all
```

## Prompts

LLM prompts are in `prompts/` for easy editing without touching pipeline code:
- `prompts/extraction.py` — agenda item extraction from PDF text
- `prompts/briefing.py` — EDITORIAL_RULES + 3-pass briefing generation prompts

## Tests

```bash
uv run python -m pytest tests/
```

## Utility Scripts

```bash
# Check if a city is fully supportable (discovery → collection → PDF quality)
uv run python scripts/check_city.py --city "Johnstown" --state OH

# Re-probe all source URLs, check for migrations
uv run python scripts/verify_city_sources.py
```

## Known Platform Blockers

| City | Platform | Blocker |
|------|----------|---------|
| Mason OH | CivicClerk | JS SPA — API returns no events without Playwright |
| Pflugerville TX | Legistar | City-side misconfiguration ("Draft Status not setup") |
| Lago Vista TX | Unknown | Migrated off CivicPlus post-2023, new platform JS-rendered |
| Mount Vernon TX | Municode | Subdomain format uses Drupal `/views/ajax`, not `/PublishPage/index` |
| Lima OH | CivicPlus | Switched to JS "interactive agendas" module post-May 2024 |
| Hartville OH | CivicPlus | Domain (hartvilleohio.com) is for sale — no website |
| Walbridge OH | CivicPlus | Site connection refused |
| Sandy Oaks TX | CivicPlus | Site connection refused |

See `docs/pilot-sync-status.md` for full status of all 49 pilot officials.

---

## Infrastructure

### S3 Bucket

All pipeline data is stored in S3. The `meeting-pipeline-dev` bucket was created manually in `us-west-2` with AES256 encryption and all public access blocked.

```
s3://meeting-pipeline-dev/
└── meeting_pipeline/
    ├── sources/{city-slug}/
    │   ├── source.json          # Discovery metadata (platform, URL, freshness)
    │   ├── data/{platform}/     # Collected meetings.json + PDFs
    │   └── constituent/         # Haystaq voter issue scores
    └── output/
        ├── meeting_queue.json
        ├── normalized/          # Per-meeting structured JSON
        └── briefings/           # Final briefing JSON per official
```

### Infrastructure TODOs

| Item | Notes |
|------|-------|
| Terraform module for S3 bucket | `meeting-pipeline-dev` was created manually. It should be codified in a Terraform module (follow patterns in `infrastructure/modules/`) so it can be reproduced for staging/prod environments and managed as IaC going forward. |
| Automate pipeline via Lambda/ECS | EventBridge cron → Lambda/ECS Fargate → collect → normalize → briefing → upsert to DB. Follow patterns in `infrastructure/modules/campaign-plan-lambda/`. |
