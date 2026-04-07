# Pipeline Status

**Last updated:** 2026-04-07
**Scope:** 49 HubSpot pilot officials across NC, OH, TX

See `docs/pilot-sync-status.md` for per-official briefing status.

---

## Pipeline Architecture

```
source_discover.py          → sources/{city}/source.json
collect_pilot_batch.py      → sources/{city}/data/{platform}/
collect_haystaq_batch.py    → sources/{city}/constituent/issue_scores.json
generate_meeting_queue.py   → output/meeting_queue.json
extract_and_normalize.py    → output/normalized/{city}_{date}.json
generate_briefing.py        → output/briefings/{city}_{date}_briefing.json
```

### Collection Agent

`collection_agent/router.py` dispatches each city to the right collector based on `source.json`.
`collect_pilot_batch.py` calls the agent for all cities in `pilot_registry.py`.

Fallback chain: dedicated collector → misc/replay → Playwright+LLM → COLLECTION_FAILED

### Pilot Registry

`pilot_registry.py` — single source of truth for officials and cities. All scripts read from it.

---

## Collector Status

| Collector | Platform | Status | Notes |
|-----------|----------|--------|-------|
| `civicclerk.py` | CivicClerk OData API | ✅ Working | Reads tenant from source.json; council_categories override supported |
| `civicplus_scraper.py` | CivicPlus AgendaCenter | ✅ Working | Auto-discovers categories; council_category_id override supported |
| `granicus_scraper.py` | Classic Granicus (RSS) | ✅ Working | SSL bypass applied |
| `granicus_scraper.py` | New Swagit (JSON API) | ⚠️ Partial | PDF viewer URLs captured, not direct packet CDN links |
| `legistar.py` | Legistar REST API | ✅ Working | legistar_slug from source.json config |
| `escribemeetings.py` | eSCRIBE | ✅ Working | POST-based PastMeetings API |
| `boarddocs.py` | BoardDocs | ✅ Working | Not used in current pilot |
| `generic_html_scraper.py` | Custom HTML | ✅ Working | Tier 3 cities |

### Known Platform Blockers

| City | Platform | Type | Blocker |
|------|----------|------|---------|
| Mason OH | CivicClerk | Technical (our pipeline) | JS SPA — API returns no events |
| Lima OH | CivicPlus | Technical (our pipeline) | JS "interactive agendas" module, needs Playwright |
| Marysville OH | Municode Library | Technical (our pipeline) | JS SPA, different product from meetings.municode.com |
| Mount Vernon TX | Municode subdomain | Technical (our pipeline) | Uses Drupal `/views/ajax`, not `/PublishPage/index` |
| Pflugerville TX | Legistar | Technical (city-side) | City API returns 400 "Draft Status not setup" |
| Lago Vista TX | Unknown | Data | Migrated off CivicPlus post-2023, new platform unknown |
| Hartville OH | CivicPlus | Data | Domain for sale — no website |
| Walbridge OH | CivicPlus | Data | Site connection refused |
| Sandy Oaks TX | CivicPlus | Data | Site connection refused |

---

## Extraction + Briefing Status

### Extraction (`extract_and_normalize.py`)

- Reads `output/meeting_queue.json`, finds agenda-ready meetings
- Extracts PDF text via PyMuPDF (up to 150 pages / 100K chars for large packets)
- Sends to Gemini 2.0 Flash with structured Pydantic output
- Large agendas (>8,000 words) use 1-sentence descriptions to avoid token limits
- 3 retries with temperature nudge on JSON parse failure
- Already-normalized meetings are skipped; use `--force` to re-run
- Prompt: `prompts/extraction.py`

### Briefing generation (`generate_briefing.py`)

- 3-pass Gemini 2.5 Flash pipeline per meeting
- Pass 1: Categorize all agenda items + priority scoring (~16s)
- Pass 2: Card content — headline, whatYouNeedToDo, askThis (~12s)
- Pass 3: Deep-dive detail per priority issue, 2-4 calls (~15s each)
- Uses Haystaq constituent scores in all 3 passes when available
- Prompts: `prompts/briefing.py` (EDITORIAL_RULES + 3 builders)
- Output: `output/briefings/{city}_{date}_briefing.json`

---

## Source Discovery

**Script:** `scripts/source_discover.py`

3-phase pipeline:
1. **Candidate discovery** — known registry → Tavily search → platform URL probes
2. **Freshness verification** — fetch URL, extract dates, 90/365-day thresholds
3. **Rank and select** — by freshness score + platform tier

Freshness values: `fresh`, `stale_warning`, `stale`, `unknown_spa`, `empty`, `blocked`

Known sources registry: `docs/discovery/known-sources-registry.json`

---

## Open Issues

| Issue | Status |
|-------|--------|
| Granicus packet URL — scraper captures viewer page, not CDN packet | ❌ Not fixed — manual workaround for Greenville NC and Kyle TX |
| Municode subdomain uses wrong API endpoint | ❌ Not fixed — affects Mount Vernon TX |
| CivicClerk SPA cities need Playwright | ❌ Not fixed — affects Mason OH |
| Granicus CloudFront URLs need extraction from viewer HTML | ❌ Not fixed |

---

## Pipeline Issues Fixed

| Issue | Fix |
|-------|-----|
| `find_best_pdf` picked agenda over packet | Sorts by "packet" in name then largest size |
| CivicPlus filenames use `YYYYMMDD` not `YYYY-MM-DD` | Glob checks both date formats |
| `agenda_posted_no_files` entries skipped even with PDF on disk | Extraction now checks for local PDF |
| Gemini truncates structured JSON on large agendas | Short descriptions + retry loop |
| Batch scripts used hardcoded city lists | Replaced by `pilot_registry.py` as single source of truth |
| `check_city.py` duplicated collector logic | Now uses `route_city()` from collection agent |
| `find_best_pdf` had hardcoded platform dirs | Now scans all `data/*/pdfs/` dynamically |
| CSV dependency for officials list | Replaced by `pilot_registry.py` |
| Platform overrides (CivicClerk categories, CivicPlus category IDs) only in batch scripts | Moved into `source.json config` block |
