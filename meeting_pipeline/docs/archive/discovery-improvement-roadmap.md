# Source Discovery Improvement Roadmap

**Goal:** If a city publishes meeting agendas anywhere on the internet, the pipeline should find and extract them automatically.

**Current precision:** 78.9% on a 19-city benchmark set. Structural ceiling with current approach: ~79%.

---

## Why the Current Pipeline Fails

Three root causes account for nearly all misses:

### 1. Bot-protected pages (Walbridge OH, Westerville OH, Statesville NC)
Akamai/Cloudflare blocks `httpx` outright. The URL is known — we just can't read it.

### 2. Undiscoverable slugs (BoardDocs cities)
`go.boarddocs.com/{state}/{slug}/Board.nsf/Public` — the slug is an opaque string (e.g., `oh/lima`) not derivable from the city name. Tavily doesn't index BoardDocs well enough to surface it.

### 3. Poor search indexing (townships, small towns, custom CMSes)
Small-city WordPress sites, township PHP pages, and Squarespace-hosted government sites don't rank well in search. Tavily returns 0 relevant results, and there's no known URL pattern to probe.

---

## Improvement Areas

### A. Playwright headless browser (highest impact)

**What it solves:** Bot-protected pages, JS-rendered SPAs (CivicClerk, PrimeGov, Diligent).

**How it works:** When `httpx` gets a 403 or returns a React shell with no dates, fall back to Playwright with a real Chromium browser and a residential-looking user agent. Playwright renders the page, executes JavaScript, and returns the actual DOM.

**Where to integrate:** As a final fallback in `verify_freshness()` — only invoked when all `httpx`-based checks return `blocked` or `unknown_spa`.

**Cost:** ~3-5 seconds per page, requires Chromium. Already used in the collection agent (`reason.py`). Reuse that infrastructure.

**Cities unblocked:** Walbridge OH, Westerville OH, Statesville NC, Mason OH, Loveland OH, La Porte TX, and any future CivicClerk/PrimeGov city where the OData API is also gated.

---

### B. BoardDocs public index scraping

**What it solves:** Unknown BoardDocs slugs for cities we know are on BoardDocs.

**How it works:** BoardDocs maintains a public committee/board index at:
```
https://go.boarddocs.com/Public
```
This page lists all public boards by state. Scrape it once, build a state → slug mapping, cache in `known-sources-registry.json`.

**Alternatively:** When a city is suspected to be on BoardDocs (e.g., discovery finds `boarddocs` in homepage HTML but no slug), try the BoardDocs search:
```
GET https://go.boarddocs.com/Public?state=OH&q={city}
```

**Cities unblocked:** Lima OH, Canal Fulton OH, Poland OH, Stallings NC, Pittsboro NC, Maple Heights OH, Pembroke NC — and any future cities.

---

### C. Smarter search queries for non-council bodies

**What it solves:** Township trustees, school boards, village councils — entities where "city council agendas" returns nothing.

**How it works:** Detect the body type from the input (city name contains "Township", "ISD", "Village") and adjust the Tavily query:

| Body type | Search query |
|-----------|-------------|
| Township trustee | `"{township} {county} {state} trustee meeting agendas minutes"` |
| School board | `"{district} school board meeting agendas minutes"` |
| Village council | `"{village} {state} village council agendas minutes"` |
| City council (default) | `"{city} {state} city council meeting agendas minutes"` |

Also pass a `body_type` hint through `process_city()` so the freshness verifier can look for "Board of Trustees" or "School Board" instead of "City Council" when checking whether content is for the right body.

---

### D. Structured government data sources

**What it solves:** Cities where no web search works at all — the URL just isn't indexed.

**Alternative data sources to check:**
- **OpenSecrets / FollowTheMoney** — links to city government sites
- **Municipal government directories** — ICMA, NLC, NACo all maintain city website directories
- **State municipal league websites** — Ohio Municipal League, NCLM (NC), TML (TX) all list member city websites with official domains
- **Google Knowledge Graph** — `{city} {state} city hall` often returns the official domain as a structured result even when Tavily doesn't

**Implementation:** Add a "directory lookup" step before Retry 2. If Tavily returns 0 results with confidence, try fetching the state municipal league member list and find the city's official domain from there.

---

### E. PDF link detection improvements

**What it solves:** Custom CMS cities (Squarespace, Weebly, generic PHP) where the agenda page exists but has no recognized platform markers.

**Current behavior:** `probe_domain_for_agendas()` detects platforms by URL pattern and page content keywords. It doesn't specifically look for PDF links.

**Improvement:** After probing each path, if no platform is detected, scan for:
- `<a href="...pdf">` links with "agenda" in the text or URL
- Links to `/DocumentCenter/`, `/files/`, `/uploads/`, `/wp-content/uploads/` containing "agenda"
- `<a>` tags with dates in the link text adjacent to "agenda" or "minutes"

If found, treat as `unknown` platform with `fresh` status if a PDF with a recent date is found. This catches Squarespace, WordPress media, and custom PHP sites that don't match any platform pattern.

---

### F. Recency-aware Tavily queries

**What it solves:** Tavily returning stale indexed pages instead of current ones.

**How it works:** Add `days_back` or `include_answer` parameters to Tavily searches in Retry 1. Tavily supports a `days` filter to limit results to recent content:

```python
tavily.search(
    query=f"{city} {state} city council agenda 2026",
    days=90,  # only results indexed in last 90 days
    search_depth="advanced",
)
```

This prevents old BoardDocs/CivicPlus URLs from indexed 2019 pages from appearing as candidates.

---

## Priority Order

| Priority | Improvement | Effort | Cities unblocked |
|----------|-------------|--------|-----------------|
| 1 | **Playwright fallback** | Medium — reuse reason.py infra | 5-8 bot-blocked cities |
| 2 | **BoardDocs index scrape** | Small — one-time scrape + registry | 7 cities |
| 3 | **Body-type-aware queries** | Small — query string logic | Townships, school boards |
| 4 | **PDF link detection** | Small — add to probe loop | Custom CMS cities |
| 5 | **Recency-aware Tavily** | Trivial — add `days=90` param | Reduces stale candidates |
| 6 | **State municipal league lookup** | Medium — scrape 3 state sites | Last-resort fallback |

---

## Expected Outcome

With improvements A-D implemented, estimated precision: **90-93%**

Remaining ~7-10% will be cities that:
- Have no public digital agenda system (Facebook-only, paper-only)
- Are behind auth walls (internal government portals)
- Have been discovered manually and should be added to the known-sources registry

The registry (`known-sources-registry.json`) is the right long-term answer for the true tail — manual curation is faster and more reliable than heuristic discovery for one-off edge cases.
