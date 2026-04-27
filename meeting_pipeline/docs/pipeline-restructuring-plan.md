# Pipeline Restructuring Plan

Prepare the meeting data pipeline for infrastructure deployment (Lambda, Fargate, Step Functions) by reorganizing code into clean, independently deployable stages with shared utilities.

## Status: COMPLETE ✅

All 6 phases implemented. Pipeline is infrastructure-ready.

---

## What Changed

### Before
- `source_discover.py`: 5,159 lines, monolithic
- `scan_meeting_schedule.py`: 974 lines, platform scanners inline
- 42 `sys.path.insert` hacks across 30+ files
- Circular imports between discovery ↔ scan
- `.env` auto-loaded by library module (`config.py`)
- No clear stage boundaries or entry points

### After

```
meeting_pipeline/
  shared/                              # Common layer (Lambda Layer candidate)
    constants.py          (380 lines)  # All pipeline constants — single source of truth
    url_utils.py          (150 lines)  # URL validation, platform detection
    date_utils.py         (170 lines)  # Date extraction, freshness classification
    discovery_helpers.py   (70 lines)  # make_candidate, safe_fetch
    generic_agenda_scanner.py (270 lines)  # Three-tier Firecrawl + Gemini scanner
    config.py                          # Re-export: AgentConfig, get_storage
    storage.py                         # Re-export: StorageBackend
    body_validation.py                 # Re-export: score_body_match, keywords
    firecrawl_client.py                # Re-export: Firecrawl utilities

  stages/
    orchestrator.py                    # Local dev runner (Step Functions replaces in prod)
    discover/
      process.py           (60 lines)  # process_one_city() entry point
      scoring.py          (170 lines)  # Candidate scoring, ranking, domain trust
      search.py           (290 lines)  # Serper, DDG, Exa, Tavily, PDF search
      crawl.py            (240 lines)  # Domain validation, Firecrawl map/crawl
    scan/
      process.py           (50 lines)  # process_one_city() entry point
      platforms/
        legistar.py        (58 lines)
        civicplus.py       (61 lines)
        boarddocs.py       (75 lines)
        civicclerk.py      (87 lines)
        granicus.py       (117 lines)
        escribe.py         (87 lines)
    collect/
      process.py           (41 lines)  # process_one_city() entry point
    extract/
      process.py           (77 lines)  # process_one_meeting() entry point
    briefing/
      process.py           (36 lines)  # process_one_meeting() entry point

  scripts/                             # Batch runners (call stage entry points)
    source_discover.py    (3,550 lines, was 5,159 — -31%)
    scan_meeting_schedule.py (484 lines, was 974 — -50%)
    extract_and_normalize.py (440 lines, unchanged)
    generate_briefing.py  (1,279 lines, unchanged)
    run_serve_users_pipeline.py (714 lines, unchanged)
```

### Key Improvements
- **Zero `sys.path` hacks** — all imports work via `uv run`
- **Zero circular imports** — shared modules are the dependency root
- **5 stage entry points** — each callable by batch runner or Lambda handler
- **`.env` only in entry points** — library modules read `os.environ` only
- **Constants in one place** — `shared/constants.py` is the single source of truth
- **Platform scanners independently testable** — one file per platform

---

## Phases Completed

| Phase | What | Result |
|-------|------|--------|
| **1a** | Create `shared/constants.py` | All constants moved, scan imports from shared |
| **1b** | Create `shared/url_utils.py` | detect_platform, is_wrong_city, is_non_agenda_url |
| **1c** | Create `shared/date_utils.py` | extract_dates, classify_freshness, parse_date_from_filename |
| **1d** | Re-export shims for config, storage, body_validation, firecrawl | Old imports still work, new code uses `shared/` |
| **1e** | Remove `sys.path` hacks | 42 hacks removed from 30+ files |
| **1f** | Remove `.env` from library modules | `config.py` no longer auto-loads, entry points handle it |
| **2** | Split `source_discover.py` | scoring, search, crawl extracted → 5,159 to 3,550 lines |
| **3** | Split `scan_meeting_schedule.py` | 6 platform scanners extracted → 974 to 484 lines |
| **4** | Extract + briefing entry points | `process_one_meeting()` for both stages |
| **5** | Collection entry point | `process_one_city()` wrapping router.py |
| **6** | Orchestrator | `run_discover()`, `run_scan()`, `run_collect()` |

---

## What's Next: Infrastructure Deployment

The code is now ready for infrastructure. Next steps:

### Lambda Handlers (10 lines each)
```python
# Example: scan Lambda handler
from meeting_pipeline.stages.scan.process import process_one_city

def handler(event, context):
    return asyncio.run(process_one_city(
        slug=event["slug"],
        source=event["source"],
        source_key=event["source_key"],
    ))
```

### Recommended Infrastructure
| Stage | Compute | Trigger |
|-------|---------|---------|
| Discover | Fargate (Playwright) | Weekly cron, per-city |
| Scan | Lambda (512MB, 5min) | Daily cron, per-city |
| Collect | Lambda + Fargate | SQS (agenda_posted events) |
| Extract | Lambda (1.5GB, 10min) | SQS (per meeting) |
| Briefing | Lambda (1GB, 10min) | SQS (per meeting) |

### Remaining Code Cleanup (Optional)
- `source_discover.py` still has 3,550 lines — probes, freshness, main flow could be further extracted
- `generate_briefing.py` at 1,279 lines is well-structured but could be split by pass
- `run_serve_users_pipeline.py` could be replaced by the `stages/orchestrator.py`
- Move `PILOT_CITIES` list to a config file or S3
- Move CSV file reads to S3 storage backend
