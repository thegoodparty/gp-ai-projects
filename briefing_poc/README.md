# Elected Official Briefing Generator

Automated pipeline that collects municipal legislative data, budget records, voter modeling, and local news — then uses LLM analysis to generate comprehensive briefing documents for newly elected officials.

## What It Does

Given a city name, the pipeline:
1. Collects legislative records (meetings, agenda items, votes, PDF attachments)
2. Collects budget/fiscal data from state open data APIs
3. Extracts text from PDF staff reports and attachments
4. Runs 6-pass LLM analysis (legislative overview, vote patterns, budget trends, document summaries, committee profiles, synthesis)
5. Collects constituent voter modeling data (demographics, issue priorities)
6. Maps legislative topics to voter issue scores
7. Identifies gaps between council priorities and constituent concerns
8. Collects local news coverage for contentious items
9. Generates actionable "quick wins" tied to priority gaps
10. Assembles a polished markdown briefing document

## Validated Cities

| City | Legislative Platform | Status |
|------|---------------------|--------|
| Charlotte, NC | Legistar (REST API) | Complete |
| Wake County, NC | Legistar (REST API) | Complete |
| Raleigh, NC | eSCRIBE (JSON API) | Complete |

## Project Structure

```
briefing_poc/
├── scripts/               # Pipeline scripts (run in order)
│   ├── city_config.py     # Config loader — all scripts import from here
│   ├── utils.py           # Shared utilities (load_json)
│   ├── 01_collect_legislative.py
│   ├── 02_collect_budget.py
│   ├── 03_extract_pdfs.py
│   ├── 04_run_analysis.py       # 6-pass LLM analysis (Gemini 2.5 Flash)
│   ├── 05_assemble_briefing.py  # Final briefing document generation
│   ├── 06_collect_constituent_data.py
│   ├── 06b_map_topics_to_issues.py
│   ├── 07_council_vs_constituent.py
│   ├── 08_collect_discussions.py
│   └── 09_generate_quick_wins.py
├── collectors/            # Data collection modules (one per platform)
│   ├── legistar.py        # Granicus Legistar REST API
│   ├── boarddocs.py       # Diligent BoardDocs (AJAX scraping)
│   ├── escribemeetings.py # Diligent eSCRIBE (JSON API)
│   ├── budget_linc.py     # NC LINC/OSBM fiscal data
│   ├── haystaq_voter.py   # Haystaq voter modeling via Databricks
│   └── pdf_extractor.py   # PDF text/table extraction
├── cities/                # Per-city config and output data
│   ├── charlotte/
│   │   ├── city_config.json
│   │   └── data/          # All collected and generated data
│   ├── wake_county/
│   └── raleigh/
└── docs/                  # Design docs and research notes
```

## Prerequisites

**API Keys** (set in `.env`):
- `GEMINI_API_KEY` — Google AI Studio (required for LLM analysis)
- `TAVILY_API_KEY` — tavily.com (optional, fallback for news search)

**Databricks** (for constituent data):
- `DATABRICKS_API_KEY`
- `DATABRICKS_SERVER_HOSTNAME`
- `DATABRICKS_HTTP_PATH`
- `AWS_PROFILE=work`

## Running the Pipeline

All scripts accept `--city` to select which city to run:

```bash
# 1. Collect legislative data (meetings, votes, PDFs)
uv run python briefing_poc/scripts/01_collect_legislative.py --city charlotte

# 2. Collect budget/fiscal data
uv run python briefing_poc/scripts/02_collect_budget.py --city charlotte

# 3. Extract text from PDF attachments
uv run python briefing_poc/scripts/03_extract_pdfs.py --city charlotte

# 4. Run 6-pass LLM analysis
uv run python briefing_poc/scripts/04_run_analysis.py --city charlotte

# 5. Collect constituent voter data (requires Databricks credentials)
AWS_PROFILE=work uv run python briefing_poc/scripts/06_collect_constituent_data.py --city charlotte

# 6. Map legislative topics to voter issue scores
uv run python briefing_poc/scripts/06b_map_topics_to_issues.py --city charlotte

# 7. Compare council priorities vs constituent concerns
uv run python briefing_poc/scripts/07_council_vs_constituent.py --city charlotte

# 8. Collect local news discussion context
uv run python briefing_poc/scripts/08_collect_discussions.py --city charlotte

# 9. Generate quick win recommendations
uv run python briefing_poc/scripts/09_generate_quick_wins.py --city charlotte

# 10. Assemble final briefing document (run last — pulls from all above)
uv run python briefing_poc/scripts/05_assemble_briefing.py --city charlotte
```

Each script is **resumable** — it checks for existing output and skips completed work. Safe to re-run.

## Adding a New City

1. Create `cities/{slug}/city_config.json` (copy an existing one as a template)
2. Fill in: legislative platform details, budget data source, Databricks filter, local news domains
3. Run the pipeline with `--city {slug}`

See `docs/city-onboarding-and-automation.md` for the full step-by-step process.

## Architecture

- **Config-driven**: All city-specific values live in `city_config.json`. Scripts are city-agnostic.
- **Pluggable collectors**: Each legislative platform has its own collector module. All produce the same output schema (`matters.json`, `events.json`, `bodies.json`, `event_items/`, `attachments/`).
- **LLM**: Gemini 2.5 Flash for all analysis. 6 structured passes with Pydantic output schemas. Cost: ~$0.04-0.06 per city.
- **Resumable**: Every script checks for existing output before running. Partial runs can be continued.

## Supported Legislative Platforms

| Platform | Type | Collector |
|----------|------|-----------|
| Legistar (Granicus) | REST API | `collectors/legistar.py` |
| BoardDocs (Diligent) | AJAX scraping | `collectors/boarddocs.py` |
| eSCRIBE (Diligent) | JSON API | `collectors/escribemeetings.py` |
