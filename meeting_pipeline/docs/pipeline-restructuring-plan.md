# Pipeline Restructuring Plan

Prepare the meeting data pipeline for infrastructure deployment (Lambda, Fargate, Step Functions) by reorganizing code into clean, independently deployable stages with shared utilities.

## Current State

- **150/199 cities** with meeting data (75% coverage)
- 4 stages: Discover → Scan → Extract → Briefing
- Code works but is organized as monolithic scripts with `sys.path` hacks
- `source_discover.py` is 5000+ lines
- Circular imports between modules
- Config loaded from `.env` file on disk
- All stages iterate over all cities in a single process

## Target State

- Each stage has a `process_one_city()` or `process_one_meeting()` function
- Batch scripts call these in a loop (current behavior)
- Future Lambda/Fargate handlers call them directly (no code changes needed)
- Clean Python package — no `sys.path` hacks, installable imports
- All config from environment variables, all data from S3
- Shared utilities in a common layer

---

## Phase 1: Foundation (shared layer + packaging)

### 1a. Create `shared/constants.py`

Move from `source_discover.py`:
- `_STATE_NAMES` (currently duplicated at two locations)
- `STATE_ABBREVS`
- `PLATFORM_PATTERNS`, `COLLECTION_METHODS`, `PLATFORM_TIER`
- `FRESHNESS_SCORE`, `SOURCE_BONUS`
- `FRESH_THRESHOLD`, `STALE_WARNING_THRESHOLD`
- `WRONG_CITY_PATTERNS`, `WRONG_ENTITY_PATTERNS`, `WRONG_DOMAIN_PATTERNS`
- `BOARDDOCS_WRONG_ENTITY_KEYWORDS`, `COUNCIL_BODY_KEYWORDS`
- `FETCH_BLOCKLIST`, `CITY_NAME_PREFIXES`, `CITY_NAME_SUFFIXES`
- `PDF_PLATFORM_SIGNALS`

Move from `scan_meeting_schedule.py`:
- `IFRAME_PLATFORM_DOMAINS`, `GENERIC_MEETING_TITLES`
- `LOOKBACK_DAYS`, `LOOKAHEAD_DAYS`, `SUPPORTED_PLATFORMS`

### 1b. Create `shared/url_utils.py`

Move from `source_discover.py`:
- `detect_platform(url)`
- `is_non_agenda_url(url)`
- `is_wrong_city(url, title, city, state)`
- `is_wrong_entity(text)`
- `city_to_slug(city)` (also duplicated in `generate_manifests.py`)
- `normalize_platform_url(url, platform)`

### 1c. Create `shared/date_utils.py`

Move from `source_discover.py`:
- `extract_dates(text)`
- `classify_freshness(most_recent)`

Move from `shared/generic_agenda_scanner.py`:
- `parse_date_from_filename(filename)`

### 1d. Consolidate shared modules

Already exists:
- `shared/generic_agenda_scanner.py` ✅
- `shared/__init__.py` ✅

Move into `shared/`:
- `body_validation.py` → `shared/body_validation.py`
- `collection_agent/config.py` → `shared/config.py`
- `collection_agent/storage.py` → `shared/storage.py`
- `collection_agent/firecrawl_utils.py` → `shared/firecrawl_client.py`

### 1e. Fix Python packaging

- Add/update `pyproject.toml` so `meeting_pipeline` is an installable package
- Remove all `sys.path.insert(0, ...)` hacks from every script
- Ensure `uv run python -m meeting_pipeline.scripts.scan_meeting_schedule` works without path manipulation
- Replace `from meeting_pipeline.collection_agent.config import ...` with `from meeting_pipeline.shared.config import ...`

### 1f. Remove `.env` / filesystem dependencies

- Remove all `load_dotenv()` calls from modules (keep only in script entry points or test runners)
- Replace CSV file reads (`Terry Users2.csv`, `serve_users.csv`) with S3 reads or env var config
- Move `config/dotgov.csv` and `config/known-sources-registry.json` loading through the storage backend

**Estimated effort:** 2-3 focused sessions. High risk — touches every file. Should be done with careful testing after each sub-step.

---

## Phase 2: Split discovery (source_discover.py → stages/discover/)

### Target structure:
```
stages/discover/
  __init__.py
  search.py         # serper_search, discover_from_serper (main Serper flow)
  validate.py       # validate_domain_for_city, cross-domain checks
  crawl.py          # firecrawl_crawl_for_agenda, firecrawl_map_agenda
  probes.py         # discover_from_probes, probe_granicus_views, etc.
  scoring.py        # candidate_score, rank_candidates, agenda_authority_score
  pdf_search.py     # discover_from_pdf_search
  process.py        # process_one_city() — the main entry point
  batch.py          # batch runner (iterates CSV, calls process_one_city)
```

### Key changes:
- `run_source_discover()` becomes `process_one_city(city, state, known_sources, expected_body) → source.json dict`
- Batch runner reads city list, fans out to `process_one_city()`
- Each sub-module is independently importable and testable
- Static data (PILOT_CITIES list) moves to a config file or S3

**Estimated effort:** 1-2 focused sessions. Medium risk — large but well-understood code.

---

## Phase 3: Split scan into platform modules

### Target structure:
```
stages/scan/
  __init__.py
  platforms/
    legistar.py     # scan_legistar()
    civicplus.py     # scan_civicplus()
    civicclerk.py    # scan_civicclerk()
    granicus.py      # scan_granicus()
    boarddocs.py     # scan_boarddocs()
    escribe.py       # scan_escribe()
  body_filter.py     # filter_by_body()
  process.py         # process_one_city() — dispatch + filter
  batch.py           # batch runner
```

### Key changes:
- Each `scan_{platform}()` function moves to its own file
- `scan_city()` becomes `process_one_city(slug, source, client, storage) → upcoming_meetings dict`
- Body filter extracted to its own function
- Remove remaining re-discovery logic from scan (candidate iteration in body validation)
- Batch runner reads source.json files, calls `process_one_city()`

**Estimated effort:** 1 session. Low risk — the platform scanners are already independent functions.

---

## Phase 4: Review and clean extraction + briefing

### Extract (`extract_and_normalize.py`)

Review for:
- Prompt quality — are we following the "narrow context per pass" principle from our QA learnings?
- Error handling — what happens when Gemini returns malformed JSON?
- Per-meeting function: `process_one_meeting(city_slug, date, pdf_key) → normalized_meeting dict`
- Cost tracking accuracy

### Briefing (`generate_briefing.py`)

Review for:
- Prompt quality — are claims grounded in source text?
- Provenance tracking — is every claim traceable to a source span?
- Pass structure — are the 3 passes (categorize, cards, details) clean and well-separated?
- Per-meeting function: `process_one_meeting(normalized_meeting, constituent_data) → briefing dict`
- Cost tracking accuracy

### Key changes:
- Extract `process_one_meeting()` from both scripts
- Review and update prompts based on QA learnings
- Ensure provenance log is produced alongside briefing output
- Add fiscal amount cross-validation

**Estimated effort:** 1-2 sessions. Medium risk — prompts need careful review.

---

## Phase 5: Collection router cleanup

### Key changes:
- `process_one_city(city, state, source) → CollectionResult`
- Platform collectors stay as-is (they're already clean)
- Separate Playwright-dependent code into its own module (clearly marked "requires Fargate")
- Remove the `replay` fallback if it's unused or unreliable

**Estimated effort:** 1 session. Low risk.

---

## Phase 6: Orchestrator

### Key changes:
- Replace `run_serve_users_pipeline.py` with a thin orchestrator that:
  1. Reads city list from S3 (not CSV on disk)
  2. Calls each stage's `process_one_city()` / `process_one_meeting()` in sequence
  3. Handles fan-out (iterate cities → iterate meetings)
  4. Reports results
- This becomes the local development runner
- In production, Step Functions replaces it

**Estimated effort:** 1 session. Low risk — it's mostly removing code and calling the stage functions.

---

## Execution Order

| Phase | What | Risk | Sessions |
|-------|------|------|----------|
| **1** | Foundation (shared + packaging) | High | 2-3 |
| **2** | Split discovery | Medium | 1-2 |
| **3** | Split scan | Low | 1 |
| **4** | Review extract + briefing | Medium | 1-2 |
| **5** | Collection cleanup | Low | 1 |
| **6** | Orchestrator | Low | 1 |

**Total: 7-10 focused sessions**

Phases 1-3 should be done first (they're the foundation). Phase 4 can be done independently. Phases 5-6 depend on 1-3.

---

## Testing Strategy

After each phase:
1. Run discovery on 5 test cities — verify source.json output
2. Run scan on those 5 cities — verify upcoming_meetings.json output
3. Run extract on 2 meetings — verify normalized JSON output
4. Run briefing on 1 meeting — verify briefing JSON output
5. Check that `uv run python -m meeting_pipeline.stages.discover.batch` works without sys.path hacks

## Definition of Done

- All `sys.path` hacks removed
- All `load_dotenv()` removed from non-entry-point files
- No circular imports
- Each stage has a `process_one_city()` / `process_one_meeting()` function
- Each stage can be imported and called independently
- All tests pass
- A future Lambda handler for any stage would be ~10 lines of boilerplate
