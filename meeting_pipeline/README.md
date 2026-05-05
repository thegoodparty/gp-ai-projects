# meeting_pipeline

Collects city council meeting agendas, extracts agenda items via LLM, and generates personalized briefings for elected officials. Deployed as Lambda + SQS + Step Function on AWS; runnable locally as a CLI.

## Pipeline stages

```
discover → scan → collect → extract → briefing → (QA — separate Lambda)
```

| Stage | What it does | Code |
|-------|--------------|------|
| **discover** | Find official agenda URL per city (Serper + Firecrawl), verify freshness, identify platform | `stages/discover/` |
| **scan** | Per platform, list upcoming meetings | `stages/scan/` |
| **collect** | Per platform, pull agenda PDFs and metadata | `stages/collect/`, `collectors/` |
| **extract** | LLM-normalize PDF → structured agenda JSON | `stages/extract/` |
| **briefing** | 3-pass Gemini generator: categorize → cards → detail pages | `stages/briefing/`, `prompts/` |
| **QA** (async) | Validates briefing claims against source — runs in `meeting_qa/` Lambda | `meeting_qa/` (separate workspace member) |

## Prerequisites

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- AWS CLI with SSO access to the `goodparty` account
- Gemini API key

## Setup

```bash
cd meeting_pipeline
uv sync
cp .env.example .env             # then edit .env per below
aws sso login --profile goodparty
aws s3 ls s3://meeting-pipeline-dev/meeting_pipeline/   # sanity check
```

### .env

| Variable | Description |
|----------|-------------|
| `GEMINI_API_KEY` | Required — LLM extraction + briefing generation |
| `STORAGE_BACKEND` | `s3` (default for prod) or `local` (for dev iteration) |
| `S3_BUCKET` | `meeting-pipeline-dev` for dev |
| `AWS_PROFILE` | `goodparty` |
| `SERPER_API_KEY` | Required for discover stage |
| `FIRECRAWL_API_KEY` | Required for discover stage |
| `DATABRICKS_*` | Required only for `collect_haystaq_batch.py` (constituent voter data) |

## Running the pipeline

The single entry point is `scripts/run_pipeline.py`. It runs all phases by default; pass `--phase` to run a subset.

```bash
# Run everything for a single city
uv run python meeting_pipeline/scripts/run_pipeline.py --city chapel-hill-NC

# Run a subset of phases
uv run python meeting_pipeline/scripts/run_pipeline.py --phase scan --phase collect

# Re-run briefings only, overwriting existing
uv run python meeting_pipeline/scripts/run_pipeline.py --phase briefing --force

# Limit to upcoming meetings only
uv run python meeting_pipeline/scripts/run_pipeline.py --phase briefing --future-only

# Dry-run (preview without writing)
uv run python meeting_pipeline/scripts/run_pipeline.py --phase extract --dry-run

# Multiple cities
uv run python meeting_pipeline/scripts/run_pipeline.py --city chapel-hill-NC --city austin-TX

# All cities from a CSV
uv run python meeting_pipeline/scripts/run_pipeline.py --csv serve_users.csv --phase extract
```

Phase-specific scripts also exist (called by `run_pipeline.py` internally; useful for debugging):
- `scripts/source_discover.py` — discover stage
- `scripts/scan_meeting_schedule.py` — scan stage
- `scripts/extract_and_normalize.py` — extract stage
- `scripts/generate_briefing.py` — briefing stage
- `scripts/collect_haystaq_batch.py` — Databricks → S3 voter scores

## Directory layout

```
meeting_pipeline/
├── stages/                  # Pipeline stages (one subdirectory per phase)
│   ├── discover/
│   ├── scan/
│   ├── collect/
│   ├── extract/
│   ├── briefing/
│   └── orchestrator.py      # Coordinates stages; called by run_pipeline.py
├── collectors/              # Per-platform agenda collectors
├── prompts/                 # LLM prompts (briefing.py, extraction.py)
├── shared/                  # Cross-stage utilities (config, storage, validation)
├── lambda_handlers/         # AWS Lambda entry points (scan, process, discover)
├── scripts/                 # CLI entry points (run_pipeline.py + others)
│   └── tools/               # Operational utilities (check_city, verify_*)
├── tests/                   # 114 unit tests
├── docs/                    # Architecture + decision docs
├── Dockerfile.lambda        # Lambda image (scan + process)
└── Dockerfile.discover      # Fargate image for the discover stage
```

`sources/` and `output/` are not in the repo — all pipeline data lives in S3.

## Storage layout

```
s3://meeting-pipeline-dev/meeting_pipeline/
├── sources/{city-slug}/
│   ├── source.json                      # Discovery metadata
│   ├── data/{platform}/                 # Collected meetings.json + PDFs
│   └── constituent/issue_scores.json    # Haystaq voter scores
└── output/
    ├── normalized/{slug}_{date}.json    # Per-meeting structured JSON
    ├── briefings/{slug}_{date}_briefing.json
    └── qa/{slug}_{date}/                # QA outputs (written by meeting_qa Lambda)
```

## Collectors

| Module | Platform | Notes |
|--------|----------|-------|
| `civicclerk.py` | CivicClerk OData API | |
| `civicplus_scraper.py` | CivicPlus AgendaCenter | AJAX-based, auto-discovers categories |
| `granicus_scraper.py` | Granicus / Swagit | Classic RSS + new Swagit JSON |
| `legistar.py` | Legistar REST API | |
| `escribemeetings.py` | eSCRIBE | POST-based JSON API |
| `boarddocs.py` | BoardDocs | AJAX scraper |
| `novus_scraper.py` | NovusAgenda | |
| `municode.py` | Municode | |

## Operational tools

```bash
# Check whether a city is fully supportable (discovery → collection → PDF quality)
uv run python meeting_pipeline/scripts/tools/check_city.py --city "Johnstown" --state OH

# Re-probe all source URLs, check for migrations
uv run python meeting_pipeline/scripts/tools/verify_city_sources.py

# Verify all generated briefings (sanity checks across S3)
uv run python meeting_pipeline/scripts/tools/verify_briefings.py
```

## Tests

```bash
uv run pytest meeting_pipeline/tests/
```

## Deployment

Application code lives on the `meeting-pipeline` branch. Infrastructure (Terraform, Dockerfiles, Lambda handlers, CI workflow) lives on the stacked `meeting-pipeline-infra` branch. CI builds the Lambda image on push to `develop` / `qa` / `prod` and updates the corresponding Lambda functions. See `infrastructure/modules/meeting-pipeline/` and `.github/workflows/build-meeting-pipeline.yml` on the infra branch for details.
