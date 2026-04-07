# Collection Agent — Scaling Analysis

*Written April 2026. Based on 67-city POC results.*

---

## Cost Model

Raw API and compute costs stay manageable even at national scale. This is not the binding constraint.

### Architecture A: Nav_config + Replay (current)

LLM runs once per city to derive a `nav_config`. Future runs use replay mode (no LLM). Cost is front-loaded; steady-state is cheap.

| Scale | One-time (discovery) | Monthly steady-state |
|---|---|---|
| 67 cities (current) | $1.34 | ~$0.01 |
| 1,000 cities | $20 | ~$1.40 |
| 5,000 cities | $100 | ~$6.90 |
| 30,000 cities | $600 | ~$97 |
| 50,000 cities | $1,000 | ~$75 |

Monthly breakdown at 50K: source re-discovery $50 + PDF egress $13 + nav_config re-reason $4 + misc $8.

**LLM cost (Gemini Flash) is negligible** — ~$0.0009 per reason-mode call. The dominant recurring cost is source re-discovery via Tavily ($0.02/city/migration), not LLM inference. However, this approach requires a nav_config quality monitoring system and engineering time to remediate stale configs at scale (see Issue 1).

### Architecture B: Agentic Browser (no nav_config)

An LLM agent drives a browser on every run — no config saved, no replay, agent adapts to whatever the page looks like. Each run costs ~5 LLM steps × Gemini Flash. Run frequency: ~4 full runs/city/month (gated by health-check probes so the agent only fires when a new meeting is expected).

| Scale | LLM | Fargate | Re-discovery | PDF egress | **Monthly total** |
|---|---|---|---|---|---|
| 1,000 cities | ~$6 | ~$8 | ~$1 | ~$1 | **~$16** |
| 5,000 cities | ~$29 | ~$12 | ~$5 | ~$3 | **~$49** |
| 10,000 cities | ~$58 | ~$18 | ~$10 | ~$5 | **~$91** |
| 30,000 cities | ~$175 | ~$24 | ~$30 | ~$13 | **~$242** |

Assumptions: 35% of cities have dedicated collectors (Legistar, CivicPlus, Granicus, CivicClerk) at near-zero cost; agentic browser runs only for the ~65% misc cities. Fargate runs overnight batch windows, ~20 concurrent workers.

### Architecture Comparison at 30K Cities

| | Nav_config + Replay | Agentic Browser |
|---|---|---|
| Monthly cost | ~$97 + engineering | ~$242 |
| Nav_config staleness | ~275 configs/month degrade silently | Eliminated — no config to go stale |
| Pagination | Requires explicit logic | Agent handles naturally |
| Website redesigns | Triggers nav_config invalidation + remediation | Agent adapts automatically |
| Lambda-compatible | Replay yes, reason mode no | No (still needs Fargate for browser) |
| Engineering overhead | High (monitoring, quality loops, remediation) | Low |

**The cost delta is ~$145/month = ~$1,750/year.** At 30K cities, the engineering time saved by eliminating nav_config maintenance almost certainly exceeds this. The agentic approach is the better long-term architecture; the nav_config approach is cheaper to operate but more expensive to maintain.

**Recommended hybrid:** Keep dedicated collectors for known platforms (covers ~35-40% of cities essentially for free). Use agentic browser for the remaining ~60-65% misc cities only.

---

## Research Findings: Landscape Survey (April 2026)

*Deep research conducted across three dimensions: existing aggregators, structured feed availability, and creative non-scraping alternatives.*

### The Core Reframe: You Are Scraping ~15 Platforms, Not 30,000 Cities

This is the most important architectural insight from the research. US municipal websites are not infinitely diverse — they are concentrated on a small number of vendor platforms:

| Platform | Estimated US Cities | Collection Method |
|---|---|---|
| CivicPlus (AgendaCenter) | ~3,500+ | Dedicated scraper / RSS probe |
| Legistar (Granicus) | ~250 large cities | **Free REST API with direct PDF URLs** |
| CivicClerk | ~800+ | **OData API before Playwright** |
| Granicus (classic + Swagit) | ~1,000+ | **RSS with `<enclosure>` PDF links** |
| NovusAGENDA | ~200+ (declining) | Direct PDF URL pattern |
| Diligent Community | ~500+ | Playwright required |
| BoardDocs | ~3,500 (mostly school boards) | HTML scrape only |
| PrimeGov | ~200+ | Dedicated scraper |
| WordPress (The Events Calendar) | ~2,000+ | RSS (dates only) + PDF scrape |
| Revize CMS | ~200+ | HTML scrape |
| DestinyHosted | ~100+ | Direct URL pattern |
| eGov/CivicSend | ~300+ | HTML scrape |
| Municode Meetings | ~300+ | Partial public API |
| Custom/legacy HTML | ~8,000–15,000 | Agentic browser |

Building a collector for each *platform* gives you coverage of hundreds to thousands of cities per implementation. Every city you add costs nearly zero marginal effort once the platform pattern is implemented.

### Feed APIs: What Already Exists Without Scraping

Several platforms offer structured access that our current implementation is not fully exploiting:

**Legistar REST API** — The `EventAgendaFile` field in `webapi.legistar.com/v1/{client}/events` is a **direct URL to the agenda PDF**. No HTML scraping, no Playwright. Already in use for our Legistar cities; the PDF URL comes for free in the event object.

**Granicus RSS** — `https://{subdomain}.granicus.com/ViewPublisherRSS.php?view_id={N}&mode=agendas` returns an RSS 2.0 feed where each `<item>` includes an `<enclosure type="application/pdf">` with the direct agenda PDF URL. Already using this for Granicus cities.

**CivicClerk OData API** — `https://{city}.api.civicclerk.com/v1/Events` returns JSON when the backend is alive. `GetMeetingFileStream(fileId={N})` fetches the PDF directly. Our current implementation jumps straight to Playwright; we should probe the OData API first and fall back to Playwright only when it returns 404.

**NovusAGENDA direct PDF** — `https://{city}.novusagenda.com/agendapublic/DisplayAgendaPDF.ashx?MeetingID={N}` serves the PDF given a numeric meeting ID extracted from the HTML listing page. No JS needed; httpx can parse the listing.

**CivicPlus RSS** — `rss.aspx` exposes an AgendaCenter feed when enabled (not always on by default). Worth probing before falling to Playwright; links to the agenda viewer page rather than directly to the PDF.

### What Already Exists (Don't Rebuild)

**civic-scraper** (Stanford / Big Local News, open source) — A Python library that already handles CivicPlus, Legistar, Granicus, CivicClerk, and PrimeGov. Claims 90,000+ agency sites in scope. Launched Agenda Watch platform in June 2023. Their implementation covers the same 5 platform patterns we've built; review before adding new collectors to avoid reinventing.

**Quorum Local** — Commercial SaaS covering 12,500+ local government sources (cities 3,000+ population). Near-real-time alerts. Enterprise pricing ($30K–$100K/year). Not useful as raw data but useful as a benchmark for what 12K-city coverage looks like.

**FiscalNote Curate** — 12,000+ local entities, 600K documents/week. Also enterprise. Same story.

**Cloverleaf AI** — Claims 30,000 government organizations — exactly our target scope. Covers agendas, minutes, speech-to-text of meeting videos. If they offer data API access, this is the fastest path to full coverage. Worth a direct inquiry.

### Source Discovery: Cheaper Alternatives to Tavily

**One-time SERP batch** (Serper.dev) — 30,000 queries at $0.30–$1.00/1K = **$9–$45 total** to classify every US municipality into a known platform or "custom." The query pattern `site:{city-domain} agenda filetype:pdf 2026` returns the top Google result. Run once; then poll the discovered URLs directly rather than re-searching weekly.

**Common Crawl Athena** — One AWS Athena query against the Common Crawl Parquet index finds all `.gov` and `.us` PDFs with "agenda" in the URL path from the last 6 monthly crawls. Cost: **~$0.10–$0.50 total**. Returns URL patterns per domain, enabling platform classification without live crawling. Limitation: 30-day lag, no JS-rendered content.

**Internet Archive CDX API** — Free. Query `web.archive.org/cdx/search/cdx?url={city-domain}/*&filter=mimetype:application/pdf&filter=original:.*agenda.*` to get historical agenda PDF URLs for any city domain. Useful for classifying a city's CMS from URL patterns alone. No live crawl required.

**State portals that replace per-city scraping:**
- **Utah** (`utah.gov/pmn`) — State law requires ALL Utah municipalities to post meeting notices here. One scrape covers every Utah city. RSS feeds available.
- **Rhode Island** (`opengov.sos.ri.gov/openmeetings`) — All 39 RI cities and towns. RSS feeds available.
- **Wisconsin** (`publicmeetings.wi.gov`) — State agencies; local optional (partial coverage).
- Texas, California, North Carolina, Ohio: No centralized state portal. Per-city scraping required.

### What Not to Build

**GovDelivery email subscriptions** — Granicus's GovDelivery notification system sends "agenda posted" emails to subscribers. Theoretically a real-time trigger. In practice: ToS prohibits automated subscriber registration, per-city subscription logistics are operationally complex, and coverage is only ~3K cities. The polling approach with health-check gating provides similar freshness without the legal risk.

**FOIA automation** — Agendas are required to be public before the meeting. FOIA is for documents that aren't already public. Not applicable.

**Civic volunteer / crowdsourced scrapers** — Open States works for 50 state legislatures. It does not scale to 30K cities (would need 600× more contributors). The platform-first approach is the only viable path at this scale.

---

## Issue 1: Nav_config Quality Decays Silently

**What it is.**
After a successful reason-mode run, a `nav_config` is saved to `source.json` and future runs use replay mode (generic HTML scraper, no LLM). This is fast and free, but the nav_config can go stale in ways that aren't immediately obvious.

**Evidence from POC.**
Belton TX replay returned 573 links, all "undated" — the year-header table format isn't handled by the generic scraper. The router counted this as a success (`events_found: 573`). Without manual inspection, you'd never know the data was unusable.

**Why it gets worse at scale.**
At 67 cities you notice. At 5,000 you don't. Nav_config staleness accumulates:
- City redesigns its agenda page (new URL structure, new HTML layout)
- Navigation changes (AgendaCenter → new CMS)
- Seasonal changes (new year creates new folders/paths)

Estimated decay rate: ~10% of misc-platform nav_configs go stale per month. At 5K cities with 55% misc, that's ~275 configs/month silently degrading.

**What needs to be built.**
A data quality feedback loop:
- After each replay run, check: did `events_found` drop >50% vs. trailing 90-day average?
- Are all dates "unknown"? Flag it.
- Auto-trigger reason mode if replay quality score falls below threshold.
- Weekly spot-check: sample 5% of replay outputs and verify at least one PDF opens correctly.

---

## Issue 2: Playwright is a Bottleneck, Not a Cost

**What it is.**
Reason mode runs a headless Chromium browser via Playwright. Each city takes 20–40 seconds. The current design runs this in-process, sequentially.

**The math.**
| Scale | Misc cities needing Playwright | Sequential time | At 50 concurrent |
|---|---|---|---|
| 1,000 | 550 | ~5 hrs | ~6 min |
| 5,000 | 2,750 | ~23 hrs | ~28 min |
| 50,000 | 27,500 | ~9 days | ~4.5 hrs |

50 concurrent Playwright instances requires ~25 GB RAM (500 MB each). That's fine on Fargate but needs an actual task queue rather than the current in-process model.

**What needs to be built.**
SQS → Fargate task queue for reason-mode jobs:
- Router pushes a `{"city": "...", "state": "..."}` message to SQS when reason mode is needed
- Fargate consumers pick up jobs, run Playwright, write results to S3
- Router polls for completion or uses an async callback pattern
- Concurrency controlled by Fargate task count, not application code

This is the main infrastructure gap between the current design and production scale. The storage abstraction and stateless function signatures are already in place; the queue is what's missing.

---

## Issue 3: Source Discovery Dominates Cost at Scale

**What it is.**
Source discovery (finding each city's agenda portal) uses Tavily web search — ~2 queries per city at ~$0.01/query. This is one-time per city but must be repeated when a migration is detected.

**The math.**
At 5% monthly migration rate:
- 1K cities: 50 re-discoveries/month = $1.00/month
- 5K cities: 250/month = $5.00/month
- 50K cities: 2,500/month = $50.00/month

At 50K cities, Tavily re-discovery becomes the single largest line item.

**What needs to be built.**

Layered discovery stack (cheapest first):

1. **Platform registries (free)** — Enumerate known tenants for CivicPlus, CivicClerk, Granicus, Legistar. Known slugs/subdomains require no search at all.
2. **State portals (free)** — Scrape Utah PMN and RI SOS portals to cover all cities in those states from a single endpoint.
3. **Common Crawl Athena (~$0.50 one-time)** — Query for all `.gov`/`.us` PDFs with "agenda" in the URL path. Cross-reference against city registry to find URL patterns without live crawling.
4. **SERP batch (~$9–$45 one-time)** — For all remaining cities, one Serper.dev query per city classifies the city into a known platform or "custom." Run once at bootstrap; re-run only when HEAD probe fails.
5. **Internet Archive CDX (free)** — For cities with unknown CMS, query historical URL patterns to classify the platform before any live crawl.
6. **Tavily (last resort)** — Only for cities where all registry and CDX approaches fail. Reduces Tavily volume by ~80% vs. current approach.

Health-check HEAD probes (already implemented) remain the primary migration signal. Discovery only re-runs when a probe fails, not on a fixed schedule.

---

## Issue 4: Platform Coverage Hits Diminishing Returns

**What it is.**
At 67 cities, 5 dedicated collectors (Legistar, CivicPlus, CivicClerk, Granicus, Escribe) cover ~58% of cities with zero LLM calls. The remaining 42% fall to misc/reason.

At larger scale, the dedicated-collector coverage fraction likely *shrinks* because:
- The major SaaS platforms (Legistar, CivicPlus) are concentrated in larger, better-resourced cities
- Smaller cities (<50K population) use custom WordPress/Drupal sites, Revize, CivicSend, eGov, or legacy HTML pages
- At 50K cities, the misc fraction may be 65–70%

**Consequence.**
More cities in misc → more Playwright runs → more nav_config maintenance burden. The reason-mode LLM is resilient but not perfect (see Belton TX first run with museum/expo URLs), and verification catches only content-type and date errors, not all hallucinations.

**What needs to be built.**

First, upgrade existing collectors to use structured APIs where available — these remove Playwright entirely for covered cities:

- **CivicClerk**: Probe `{city}.api.civicclerk.com/v1/Events` before launching Playwright. When the OData backend is alive, extract `fileId` values and call `GetMeetingFileStream(fileId={N})` directly for PDF bytes. Reserve Playwright only for dead-backend cities.
- **NovusAGENDA**: Parse the `/agendapublic/` HTML listing page (no JS needed), extract `MeetingID` integers, then construct `DisplayAgendaPDF.ashx?MeetingID={N}` URLs directly.
- **CivicPlus**: Probe `rss.aspx` before the full AgendaCenter Search scrape. If the AgendaCenter RSS feed is listed, use it (single HTTP fetch vs. Playwright session).

Then expand dedicated collectors to cover the next platform tier:
- **Revize CMS** (`revize.com`) — 200+ cities, consistent HTML structure
- **eGov/CivicEngage** — common in mid-size cities
- **Municode Meetings** — has a partial public API (`meetings.municode.com/api/v1/public/`)
- **NovusAGENDA** — direct PDF URL pattern (declining platform, cities migrating to CivicClerk)

Review **civic-scraper** (Stanford / Big Local News, GitHub: `biglocalnews/civic-scraper`) before building any new collector — they have working implementations for CivicPlus, Legistar, Granicus, CivicClerk, and PrimeGov that may be directly reusable.

Targeting 12–15 total dedicated collectors (including API-upgraded existing ones) would push coverage to 75–80% at national scale.

---

## Issue 5: Pagination Is Detected But Not Acted On

**What it is.**
The LLM returns `has_pagination: true` when it sees "next page" links. The current reason mode ignores this flag and only processes the first page.

**Evidence from POC.**
Greensboro NC (Escribe) returned only 4 links from the portal landing page — not because there are only 4 meetings, but because the portal paginates and only shows recent items on the first page.

At 67 cities this is minor. At scale, cities with paginated agenda archives will consistently under-collect: only the most recent meeting (or none) will be found, and the nav_config saved will be incomplete.

**What needs to be built.**
When `has_pagination` is true:
1. Identify the "next page" link from the LLM output or DOM
2. Load subsequent pages (cap at 3–5 pages to stay within the lookback window)
3. Merge events across pages before verification
4. Store `max_pages` in nav_config so replay mode knows to paginate too

---

## Issue 6: Agentic Browser as Long-Term Architecture

**What it is.**
The current reason mode is a one-shot operation: Playwright loads a page, takes a screenshot, Gemini reads it, outputs agenda links. This is not truly agentic — it can't click, paginate, or adapt mid-run.

An agentic approach (e.g., `browser-use` Python library) gives the LLM full browser control. The agent sees the page, decides what to click, executes it, observes the result, and continues until it has what it needs. You give it a goal: *"find agenda documents for city council meetings in the last 90 days"* — it figures out the navigation itself.

**Why this matters at scale.**
- Nav_config staleness (Issue 1) disappears — no config to go stale, agent adapts every run
- Pagination (Issue 5) is handled naturally — agent sees "Next" and clicks
- Website redesigns no longer require manual remediation
- The monitoring and quality-feedback systems described in Issues 1 and 5 are no longer needed

**What it still requires.**
An agentic browser still needs Playwright underneath — which means Fargate, not Lambda. The infrastructure requirement doesn't change. Browserbase (cloud-hosted browser service) is an alternative that offloads infrastructure but costs ~$500-1,000+/month at 30K-city scale, making self-hosted Fargate more economical.

**What needs to be built.**
Replace `misc/reason.py` with an agentic driver:
- Use `browser-use` Python library with Gemini Flash as the driving LLM
- Goal prompt: extract agenda links for city council meetings within the lookback window
- Agent handles pagination, JS rendering, and dynamic navigation automatically
- On success: return structured event list (same `CollectionResult` interface)
- No nav_config derived or saved — every run is fresh

The `CollectionResult` interface and router are unchanged. Only the internals of reason mode change.

---

## Summary: What to Build Before Each Scale Threshold

### Recommended Build Sequence

**Before 1,000 cities:**
1. Upgrade CivicClerk collector to probe OData API before Playwright
2. Add NovusAGENDA direct PDF URL pattern (no Playwright)
3. Add CivicPlus RSS probe as first-pass before Playwright Search
4. Run one-time Common Crawl Athena query (~$0.50) to bootstrap city URL patterns
5. Run one-time SERP batch via Serper.dev (~$30) to classify all target cities into platforms
6. Add nav_config quality monitoring (events_found trend + unknown-date alert)

**Before 5,000 cities:**
7. SQS → Fargate queue for Playwright (or prototype agentic browser-use to replace it)
8. Pagination support in reason mode (act on `has_pagination` flag)
9. Scrape Utah PMN and Rhode Island SOS as single-source state coverage
10. Add 2–3 more dedicated platform collectors (Revize, eGov, Municode Meetings)

**Before 10,000 cities:**
11. Evaluate Cloverleaf AI data partnership (claims 30K orgs, exactly our scope)
12. Multi-region Fargate deployment; SQS → Fargate with spot instances
13. Automated nav_config invalidation (or switch to agentic to eliminate the problem)

**Before 30,000 cities:**
14. Dedicated collectors covering 75%+ of cities
15. Agentic browser for remaining misc cities (if not already adopted earlier)
16. Assess data licensing from FiscalNote/Quorum for top 12K cities as cost vs. build comparison

### The Architectural Decision Point

The choice between nav_config+replay and agentic browser is ultimately a **maintenance cost vs. compute cost** tradeoff. At 30K cities:

| | Nav_config + Replay | Agentic Browser |
|---|---|---|
| Monthly compute | ~$97 | ~$242 |
| Engineering overhead | High (monitoring, remediation) | Near-zero |
| Platform API upgrades | Still required | Still required |
| Eliminates Issues 1, 5 | No | Yes |

**Recommended path:** Implement the API upgrades (CivicClerk OData, NovusAGENDA direct URL, CivicPlus RSS probe) first — these are free wins regardless of architecture. Then prototype agentic browser on 20–30 misc cities and measure real accuracy and cost. Make the architectural call at the 1,000-city threshold with actual data.
