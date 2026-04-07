# Automated Collection Agent — Plan

## Problem

The current pipeline has two brittle points:

1. **Source discovery is manually maintained.** When a city migrates platforms, nothing detects it until collection fails. Fixing each city requires manual investigation (finding the new URL, testing it, updating the registry).

2. **Collection assumes a known platform.** Cities on unknown or unsupported platforms are either skipped or fall back to non-portal sources (news articles, news emails). There's no systematic way to collect from them in the meantime.

The goal of this plan is to replace both manual steps with an agent loop that:
- Finds where city meeting data lives and verifies it's current
- Selects the right collector, or falls back to a misc collector for unknown structures
- Records what it learned so subsequent runs are cheap and deterministic
- Surfaces when new dedicated collectors should be built

---

## What Already Exists

Before building, it's important to note that two of the five planned components are largely already implemented in the existing codebase. The build plan should adapt these rather than rewrite them.

### `collectors/generic_html_scraper.py` — already the replay engine

This is a fully working config-driven scraper with five extraction strategies:

| Strategy | What it does |
|----------|-------------|
| `direct_pdf` | Find `<a href="*.pdf">` links matching a keyword |
| `document_center` | CivicPlus `/DocumentCenter/View/{id}` pattern |
| `archive_aspx` | CivicPlus `Archive.aspx?ADID={id}` pattern |
| `two_hop` | Index page → subpages → PDFs (used by Cuyahoga Falls Drupal) |
| `rss_feed` | Parse RSS for PDF links |

`GenericScraperConfig` is already essentially the nav_config schema:
```python
url, strategy, selector, keyword_filter, lookback_days, follow_url, verify_ssl
```

It also has basic document validation already: checks `%PDF` header, rejects files < 1KB.

**Gap:** City configs live in a hardcoded Python dict in `collect_generic_batch.py` instead of per-city `source.json` files. Externalizing these is the key change needed.

### `collectors/playwright_llm_scraper.py` — already the reason engine

This does exactly what the misc collector reason mode needs:
1. Playwright renders the page (networkidle → domcontentloaded fallback)
2. Takes a full-page screenshot
3. Extracts all links from the rendered DOM
4. Sends screenshot + link list to LLM (currently Gemini Flash at ~$0.01–0.05/city)
5. LLM returns structured output: `link_index`, `date`, `body`, `description`
6. Downloads identified PDFs and saves HTML agendas

The `PageAnalysis` schema even handles pagination (`has_pagination` field).

**Gap:** After identifying agenda links it downloads documents but does not record *how* it found them as a replayable config. Adding that output — translating the LLM's findings into a `GenericScraperConfig` saved to `source.json` — is the one missing step.

### The bridge between them

```
playwright_llm_scraper.py          generic_html_scraper.py
(reason: LLM figures it out)  →??→  (replay: config executes it)
                               ↑
                    missing: config generation step
```

After a successful LLM identification session, translate the findings into a `GenericScraperConfig` dict and write it to the city's `source.json`. On the next run, load that config and execute mechanically — no LLM needed.

### Collectors to keep as dedicated collectors

Based on reviewing all 14 collector scripts, the following are solid and should remain as dedicated collectors wrapped by the router:

| Collector | Status |
|-----------|--------|
| `legistar.py` | Keep — REST API, dynamic, covers most cities |
| `civicplus_scraper.py` | Keep — dynamic from source.json, handles CivicPlus variants |
| `civicclerk.py` | Keep — merge with `collect_civicclerk_spa.py` (SPA mode becomes internal fallback) |
| `granicus_scraper.py` | Keep — handles both classic Granicus and Swagit variants |
| `collect_municode_batch.py` | Keep — 3 cities, stable platform |
| `collect_primegov_batch.py` | Keep — 1 city now, but PrimeGov is common in TX |
| `escribemeetings.py` | Keep — Greensboro NC + La Porte TX, real API |

### Collectors to fold into misc collector

| Script | Disposition |
|--------|-------------|
| `collect_generic_batch.py` | Becomes misc collector replay mode (externalize configs to source.json) |
| `collect_playwright_llm_batch.py` | Becomes misc collector reason mode (add config generation step) |
| `collect_adhoc_batch.py` | City configs migrate to source.json nav_configs |
| `collect_hickory_nc.py` | Becomes a nav_config entry |
| `collect_belton_tx.py` | Becomes a nav_config entry |
| `collect_euclid_oh.py` | Has hardcoded auth token — flag for manual handling |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   SCHEDULED DISCOVERY                        │
│  Runs daily/before collection. Validates all source.json     │
│  entries, detects migrations, updates registry proactively.  │
└─────────────────────┬───────────────────────────────────────┘
                      │ source.json validated + fresh
                      ▼
┌─────────────────────────────┐
│      Collector Router       │  ← maps platform → collector
└────────────┬────────────────┘
             │
     ┌───────┴────────┐
     ▼                ▼
┌─────────┐    ┌──────────────────┐
│Dedicated│    │  Misc Collector  │  ← replay mode (has nav_config)
│Collector│    │                  │    reason mode (no config or replay failed)
│(Legistar│    └────────┬─────────┘
│CivicPlus│             │ on first success: writes nav_config to source.json
│Granicus │             ▼
│etc.)    │    ┌──────────────────┐
└────┬────┘    │ Document Verifier│  ← fetches actual PDF, confirms real content
     └────┬────┘
          │
          ▼
┌─────────────────────────────┐
│   Output + Notification Log  │  ← structured docs + health signals
└─────────────────────────────┘
```

---

## Health Monitoring vs. Error Handling

These are two different failure modes at two different points in the pipeline. Both are needed, but they serve distinct purposes and should not be conflated.

### Automated Discovery (proactive — runs on a schedule)

Detects problems *before* collection runs. The discovery agent validates every city's `source.json` on a schedule (daily or before each collection run):

- Is the source URL still reachable?
- Does it still have recent content (within 90 days)?
- Has the city migrated to a new platform?

If a source has moved or gone stale, discovery updates `source.json` automatically. Collection then runs with confidence against a verified, current source.

**This is the more valuable of the two.** If discovery is running properly, collection failures should be rare.

### Collector Fallback Chain (reactive — handles runtime surprises)

For errors that slip past discovery (transient HTTP failures, structure changes between discovery and collection, etc.), the collector has a fallback ladder:

```
1. Dedicated collector (or misc replay)
        │ fails
        ▼
2. Retry same strategy (up to 3x, with backoff)
        │ still fails
        ▼
3. Misc collector replay mode
   (try generic_html_scraper with existing nav_config)
        │ replay config broken / no config
        ▼
4. Misc collector reason mode
   (playwright + LLM re-navigates, produces new config)
        │ reason mode fails
        ▼
5. Flag COLLECTION_FAILED → trigger re-discovery
   (maybe the source genuinely moved — let discovery fix it)
        │ re-discovery also fails
        ▼
6. Log NO_SOURCE, notify for manual review
```

**Key principle:** Steps 1–4 happen within a single collection run. Step 5 escalates back to the discovery layer — the collection run does not try to do full re-discovery inline. Step 6 is the human escalation path.

### Where NOT to add fallbacks

The temptation is to add fallbacks everywhere — "if CivicPlus fails, try Granicus." Don't. Fallbacks across different platforms add noise and can silently collect wrong data. The fallback chain above escalates from "try harder with the known source" to "admit we don't know the source and ask discovery to figure it out." Cross-platform fallbacks short-circuit that and produce unpredictable results.

---

## Components

### 1. Discovery Agent
Wraps the existing `source-discover` skill with LLM reasoning. Validates that the cached `source.json` is still working before re-running discovery. When a source is stale or broken, the agent reasons through re-discovery and updates the registry.

**Adds over current skill:**
- Validates existing `source.json` before searching (fast path for stable cities)
- LLM interprets ambiguous page structures instead of failing to "unknown"
- Detects platform migrations automatically, flags them in output
- Updates `source.json` in place when a better source is found

### 2. Collector Router
Maps a platform string to the correct collector. Falls through to `MiscCollector` for any platform not in the registry.

```python
COLLECTOR_REGISTRY = {
    "civicplus":  CivicPlusCollector,
    "legistar":   LegistarCollector,
    "granicus":   GranicusCollector,
    "civicclerk": CivicClerkCollector,
    "escribe":    EScribeCollector,
    "municode":   MunicodeCollector,
    # swagit, diligent, google_drive, unknown → MiscCollector
}
```

### 3. Misc Collector
Handles any city where no dedicated collector exists. Adapts `generic_html_scraper.py` (replay) and `playwright_llm_scraper.py` (reason) — see "What Already Exists" above.

**Replay mode:**
- Loads `nav_config` from `source.json` (migrated from CITY_CONFIGS dict)
- Executes using `generic_html_scraper.py` — no LLM
- Tracks `replay_success_count` and `last_replay_at`
- Falls back to reason mode if replay fails

**Reason mode:**
- Adapts `playwright_llm_scraper.py` — Playwright + LLM vision
- Identifies agenda links, downloads documents
- **New step:** translates findings into a `GenericScraperConfig` and saves to `source.json`
- On next run, replay mode takes over

**Nav config schema** (maps to `GenericScraperConfig` fields):
```json
{
  "platform_guess": "wordpress_calendar",
  "entry_url": "https://...",
  "strategy": "direct_pdf | document_center | archive_aspx | two_hop | rss_feed",
  "selector": "a[href*='/files/city-council']",
  "keyword_filter": "agenda",
  "follow_url": null,
  "verify_ssl": true,
  "body_name_hint": "City Council",
  "recorded_at": "2026-04-01",
  "replay_success_count": 3,
  "last_replay_at": "2026-04-01"
}
```

### 4. Document Verifier
After collection, verifies that fetched documents contain real meeting content. Extends the basic `%PDF` + size check already in `generic_html_scraper.py`.

**Checks:**
- File size > 1KB (already implemented)
- PDF text extraction returns > 100 characters (not a scanned image)
- Contains meeting keywords: "agenda", "minutes", "ordinance", "motion", "resolution"
- Meeting date in document matches listing date (within ± 3 days)
- Body name matches expected (City Council vs. BZA vs. Planning)

**Output per document:**
```json
{
  "url": "https://...",
  "verified": true,
  "meeting_date_in_doc": "2026-03-24",
  "body_detected": "City Council",
  "keyword_hits": ["agenda", "ordinance", "motion"],
  "notes": ""
}
```

### 5. Notification + Logging Layer
Structured log output from every pipeline run.

**Log event types:**
```
COLLECTION_FAILED   | city=Hamilton OH   | stage=replay | falling_back_to_reason
COLLECTION_FAILED   | city=Euclid OH     | stage=reason | triggering_rediscovery
COLLECTOR_NEEDED    | platform=swagit    | cities=[Fairborn OH] | count=1
NO_PORTAL           | city=Westerville OH | best_source=news_article
MIGRATION_DETECTED  | city=Stow OH       | old=stow.oh.us | new=stowohio.gov
MISC_USED           | city=Hamilton OH   | platform_guess=google_drive | docs=4 | verified=4
VERIFY_FAILED       | city=Durham NC     | reason=scanned_pdf | ocr_needed=true
REPLAY_FAILED       | city=Hamilton OH   | reason=config_stale | falling_back_to_reason
```

Aggregate `COLLECTOR_NEEDED` events to surface collector build priorities:
> "3 cities on `diligent` platform using misc collector — consider a dedicated Diligent collector."

---

## Collector Maturity Model

| Level | Description | What runs |
|-------|-------------|-----------|
| L0 | No portal exists | Skip, log `NO_PORTAL`, flag for manual curation |
| L1 | Misc, reason mode each run | LLM navigates every run (slow, expensive) |
| L2 | Misc, replay mode | Recorded config replays mechanically (fast, cheap) |
| L3 | Template-based | Config-driven, no LLM, common pattern parameterized |
| L4 | Dedicated collector | Platform-specific code, fastest, most reliable |

New cities start at L0–L1. Dedicated collectors should be built when 3+ cities share the same `platform_guess` in misc collector logs.

---

## Build Order

### Phase 1 — Collector Interface + Router
**What:** Define the `BaseCollector` interface. Wrap existing dedicated collectors to implement it. Build the `CollectorRouter` that maps platform strings to collector instances.

**Why first:** Establishes the contract everything else plugs into. Existing collectors don't change behavior — they just get a consistent interface.

**Deliverable:** `collector_router.py`. All existing dedicated collectors implement `BaseCollector`. Unknown platforms fall to `MiscCollector` stub.

---

### Phase 2 — Document Verifier
**What:** Extend the basic PDF validation already in `generic_html_scraper.py` into a standalone verifier that checks content, keywords, date match, and body name.

**Why second:** Independently useful immediately — catches scanned PDFs, empty documents, and body name mismatches across all collectors. No dependency on later phases.

**Deliverable:** `document_verifier.py`. Takes a URL + optional expected body/date, returns structured verification result. Callable from any collector.

---

### Phase 3 — Misc Collector (Replay Mode)
**What:** Adapt `generic_html_scraper.py` into the replay engine. Externalize the CITY_CONFIGS dict from `collect_generic_batch.py` into per-city `source.json` nav_config fields. Add `misc_collector_config` to the `source.json` schema.

**Why third:** `generic_html_scraper.py` is already working — this is mostly a migration of where configs live, not a rewrite. Validates the nav_config schema against real cities before building the LLM reasoning that generates those configs.

**Deliverable:** `misc_collector.py` with replay mode. Nav configs migrated for all cities currently in `CITY_CONFIGS`. `collect_generic_batch.py` and `collect_adhoc_batch.py` become thin wrappers or are retired.

---

### Phase 4 — Discovery Agent (Registry Validation + Self-Healing)
**What:** Add LLM reasoning layer to the existing `source_discover` skill. Agent validates cached `source.json` on each run. If stale or broken, re-runs discovery, reasons through results, updates registry.

**Why fourth:** Highest-value change for reducing manual maintenance. Cities that migrate platforms get detected and fixed automatically. Builds on existing Tavily/httpx/Playwright tools — no new dependencies.

**Deliverable:** `discovery_agent.py`. Validates `source.json`, re-discovers if needed, updates with `migration_detected` flag. Runs on a schedule independently of collection.

**Model guidance:**
- Haiku: platform classification, freshness threshold checks
- Sonnet: ambiguous structure, novel platforms, migration diagnosis

---

### Phase 5 — Misc Collector (Reason Mode)
**What:** Adapt `playwright_llm_scraper.py` into the reason engine. Add the config generation step: after successful LLM identification, translate findings into a `GenericScraperConfig` and write to `source.json`.

**Why fifth:** `playwright_llm_scraper.py` is already working — the one missing piece is writing a nav_config on success. After that, replay mode (Phase 3) takes over on subsequent runs automatically.

**Deliverable:** Reason mode added to `misc_collector.py`. When reason mode completes successfully, city automatically graduates from L1 to L2 (replay mode) on the next run. `collect_playwright_llm_batch.py` retired.

**Model guidance:** Keep Gemini Flash for LLM vision (already integrated, $0.01–0.05/city). The cost advantage over Claude vision is significant for batch runs.

---

### Phase 6 — Notification + Logging Layer
**What:** Structured log output from the full pipeline. Aggregate `COLLECTOR_NEEDED` events. Add `NO_PORTAL`, `MIGRATION_DETECTED`, `COLLECTION_FAILED` to the batch summary.

**Why last:** Meaningful only once the full pipeline is in place.

**Deliverable:** Structured logging utilities. Updated `discovery-summary.json` schema. Optional CLI report showing collector health and build recommendations.

---

## What Stays the Same

- Existing dedicated collector implementations — no behavior changes
- `source_discover.py` — discovery agent wraps it, doesn't replace it
- `known-sources-registry.json` — becomes starting hints (cache), not ground truth
- Tavily, httpx, Playwright — reused as tools
- Gemini Flash for LLM vision in reason mode — cost advantage too large to replace

---

## Key Decisions to Resolve Before Building

| Decision | Options | Implication |
|----------|---------|-------------|
| Nav config storage | In `source.json` vs. separate `misc_config.json` | Separate file is cleaner; inline is simpler. Either works. |
| Replay failure threshold | Fail on first error vs. after N failures | N=3 avoids transient failures triggering expensive re-reasoning |
| Discovery schedule | Before every collection run vs. daily independent | Daily independent is more responsive to migrations |
| Re-discovery on collection failure | Inline during collection vs. separate scheduled pass | Separate pass is cleaner — collection shouldn't do re-discovery inline |
| LLM for vision | Keep Gemini Flash vs. switch to Claude | Gemini Flash at $0.01–0.05/city is hard to beat; switch only if consolidation matters |
| `NO_PORTAL` cities | Skip silently vs. always attempt + log | Always attempt + log is better for observability |

---

## File Layout

```
briefing_poc/
  scripts/
    collector_router.py          ← Phase 1 (new)
    document_verifier.py         ← Phase 2 (new)
    misc_collector.py            ← Phase 3 (replay) + Phase 5 (reason)
                                    adapts generic_html_scraper + playwright_llm_scraper
    discovery_agent.py           ← Phase 4 (new, wraps source_discover.py)
  collectors/
    generic_html_scraper.py      ← keep, used by misc_collector replay mode
    playwright_llm_scraper.py    ← keep, used by misc_collector reason mode
    legistar.py                  ← keep
    civicplus_scraper.py         ← keep
    civicclerk.py                ← keep (merge SPA fallback in)
    granicus_scraper.py          ← keep
    escribemeetings.py           ← keep
    [others]                     ← keep
  docs/v2/
    skills/
      source-discover/
        SKILL.md                 ← existing (update with agent/schedule mode)
        known-sources-registry.json
      misc-collector/
        SKILL.md                 ← new
        nav-config-schema.json   ← Phase 3 artifact
  sources/
    {city}-{state}/
      source.json                ← extended with misc_collector_config field
```
