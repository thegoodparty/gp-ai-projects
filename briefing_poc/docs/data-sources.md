# Data Sources

Where the pipeline's data comes from, what's available, quirks discovered during the POC, and what to expect when expanding.

---

## Legislative Data

Legislative data is the core input — meetings, agenda items, votes, and PDF attachments. Each city uses one of several meeting management platforms, and we have a pluggable collector for each.

### Legistar (Granicus) — ~1,800 cities

The most important platform. REST API, public, no authentication required. Used by 25 of the 50 largest US cities.

**What it provides:**
- Bodies (committees, boards)
- Events (meetings with dates and agendas)
- Matters (legislation items with titles, types, statuses)
- Event items (individual agenda items with action text)
- Votes (roll-call records with individual member votes)
- Persons (council members, staff)
- Matter attachments (PDF staff reports, budget docs)

**API details:**
- Base URL: `https://webapi.legistar.com/v1/{client}/`
- No authentication required
- OData-style filtering: `$filter=EventDate ge datetime'2025-08-24'`
- Pagination: `$top`/`$skip`
- Rate limit: 1-2 req/sec is safe (not officially published)

**Discovery:** Try `https://webapi.legistar.com/v1/{slug}/bodies` with slug patterns like `{city}{state}`, `{city}`, `{county}`. A 200 response = confirmed.

**Quirks discovered:**
- Client slugs aren't standardized — Charlotte is `charlottenc` (not `charlotte`)
- The API drops connections after ~400 sequential requests. Fixed with retry logic (exponential backoff)
- Persons endpoint returns 1,000 results (API limit) including historical members and staff
- Only 5 formal roll-call votes in 6 months for Charlotte — most votes are unanimous consent captured only in `EventItemActionText`
- ~19 out of 870 PDF attachment URLs returned 404 (metadata exists but file is missing)

**Collector:** `collectors/legistar.py`

### eSCRIBE Meetings (Diligent) — ~200+ cities

JSON API for meetings, HTML agenda pages for items, direct PDF downloads. Used by municipalities across US and Canada (originally Canadian-focused, expanding in US).

**What it provides:**
- Meetings with dates and meeting types
- Agenda items (parsed from HTML agenda pages)
- PDF documents (agendas, minutes) via direct download
- No structured vote data
- No person/member data

**API details:**
- POST to `{base_url}/MeetingsCalendarView.aspx/PastMeetings` with JSON body
- GET HTML agendas at `{base_url}/Meeting.aspx?Id={uuid}&Agenda=Agenda`
- PDF download: `{base_url}/FileStream.ashx?DocumentId={id}`
- No authentication required

**Discovery:** Try `https://pub-{city}{state}.escribemeetings.com`. If the page loads = confirmed.

**Quirks discovered:**
- Meeting types are free-text strings (e.g., "City Council Meeting - First Tuesday - Afternoon & Evening Sessions"), vary per city, and must be discovered by scraping the calendar page JavaScript
- Uses `verify=False` for SSL — some deployments use certificates that don't validate in all environments
- Timestamps are JavaScript format: `/Date(1770123616233)/`

**Collector:** `collectors/escribemeetings.py`

### BoardDocs (Diligent) — ~1,200 municipalities (6,100 orgs total, mostly school boards)

AJAX POST endpoints on a Lotus Domino/Notes backend. HTML responses parsed with HTMLParser.

**What it provides:**
- Meetings and agenda items
- PDF attachments (limited)
- No structured vote data
- No person/member data

**Quirks discovered:**
- Uses configured `committee_id` rather than auto-discovering committees
- Raleigh migrated away from BoardDocs to eSCRIBE in July 2025 — the BoardDocs config is retained for archival access only

**Collector:** `collectors/boarddocs.py`

### Key Limitation: Non-Legistar Vote Data

BoardDocs and eSCRIBE do not expose structured vote records. For these cities, vote analysis (Pass 2) relies on whatever text appears in agenda items rather than formal roll-call records. Verifiability is lower for non-Legistar cities.

---

## Budget / Fiscal Data

### NC LINC/OSBM (Current — All NC Cities)

The NC Local Government Information Network for Communities, run by the Office of State Budget and Management. Uses Opendatasoft (not Socrata or ArcGIS).

**What it provides:**
- 44 years of fiscal data (1980-2024)
- 48 variables: revenue sources, expenditure categories, debt, property valuations, per-capita metrics
- Property tax rates per $100 of assessed valuation

**API details:**
- Base URL: `https://linc.osbm.nc.gov/api/explore/v2.1/`
- Datasets: `government` (fiscal), `property-tax-rate`
- SQL-like filtering: `where=area_name='Charlotte'`
- Pagination: `offset` + `limit` (max 100 per request)
- No authentication required

**Key variables:** Ad Valorem Tax Levy, Local Option Sales Tax, Public Safety expenditure, Transportation expenditure, Total General Fund Revenue/Expenditure, General Fund Balance, Total Assessed Valuation, Population, per-capita metrics.

**Quirks discovered:**
- Max 100 records per request (not 5,000 — that returns 400 Bad Request)
- Charlotte had 1,826 fiscal records requiring 19 paginated API calls
- Municipality names must match exactly (case-sensitive)
- 2024 data may be preliminary/estimated rather than audited actuals
- LINC wasn't widely known as a REST API source — we discovered it by working backward from the NC state data ecosystem

**What's missing from LINC:**
- No department-level budget detail (Public Safety total, but not Police vs. Fire)
- No capital budget / CIP data
- No fund balance breakdown (General Fund only, not Enterprise/Special Revenue funds)
- Annual data only — no month-to-month

**Collector:** `collectors/budget_linc.py`

### Other States (Not Yet Built)

Each state has different open fiscal data sources:

| State | Source | API? |
|-------|--------|------|
| CA | State Controller's Office | Yes (Socrata) |
| NY | OpenBudget NY | Yes (Socrata) |
| TX | Texas Comptroller | Partial (some in PDFs) |
| FL | FL Dept of Revenue | Partial (property tax available) |
| Many states | No centralized source | Would need city-level budget PDFs |

**Approach for expansion:** Identify top states by candidate volume, build budget collectors for those (1-2 days each). For states without APIs, skip the budget section or extract from city budget PDFs via LLM.

---

## PDF Extraction

Staff reports, budget presentations, meeting minutes, and zoning materials are downloaded as PDFs and extracted into structured JSON.

### Approach

Two-pass extraction per PDF:
1. **Text extraction** (PyMuPDF/fitz) — fast, works for digitally-created PDFs
2. **Table extraction** (pdfplumber) — analyzes visual layout for table structures

### Charlotte Results (870 PDFs, 1.8 GB)

| Metric | Value |
|--------|-------|
| Successfully processed | 869 of 870 |
| Corrupted (couldn't open) | 1 |
| No extractable text (scanned images) | 62 |
| Total characters extracted | 9,172,514 |
| Total tables found | 4,098 |
| Processing time | 38 minutes |
| Success rate (text extraction) | 93% |

### Text Quality by Document Type

| Document Type | Quality | Notes |
|---------------|---------|-------|
| Staff reports | Excellent | Clean text, well-structured — most useful for analysis |
| Budget presentations | Good | Tables extract well, some formatting artifacts |
| Meeting minutes | Good | Complete proceedings record |
| Zoning staff analyses | Good | Includes community feedback summaries |
| Maps and site plans | Poor/None | Image-only, no text layer |
| Engineering drawings | Poor/None | Technical drawings are images |

### What's Not Handled

- **Scanned documents** — 62 PDFs with no text layer. Would need OCR (Tesseract/Docling). Low impact since these are mostly maps and drawings.
- **Complex table layouts** — Merged cells, multi-level headers, spanning columns sometimes produce messy output. Text extraction still captures the content.
- **Semantic chunking** — We split by page, not by logical section. The LLM handles this during analysis.

---

## Constituent / Voter Data

### Haystaq DNA Voter Modeling (via Databricks)

Haystaq (an L2 product) provides modeled voter attitude scores based on voter file demographics, consumer data, and survey calibration. Accessed via Databricks SQL.

**Three tables queried per city:**

| Table | Data |
|-------|------|
| `stg_dbt_source__l2_s3_{state}_uniform` | Demographics: age, party registration, address, zip |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_scores` | Issue scores (0-100 scale): transit support, housing, environment, etc. |
| `stg_dbt_source__l2_s3_{state}_haystaq_dna_flags` | Binary flags (used for some metrics) |

**Filtering:** `Residence_Addresses_City = 'CHARLOTTE'` for cities, `Residence_Addresses_County = 'WAKE'` for counties.

**Output (3 JSON files):**
- `demographics.json` — total voters, age distribution, party registration, ideology scores
- `issue_scores.json` — 15 issue dimensions with averages, tiers (Strong/Moderate/Weak)
- `zip_breakdown.json` — per-zip voter counts and demographics

**Key findings:**
- Haystaq columns are **identical across all NC cities** — same 15 issue score columns from a national dataset
- The per-city `topic_to_issue_map` varies because council topic names differ per city — auto-generated by script 06b
- NC doesn't have "Independent/Unaffiliated" voter registration — those voters show up under `other`

**Correct column names** (the original runbook had wrong names):

| Runbook Used (WRONG) | Actual Column (CORRECT) |
|----------------------|------------------------|
| `hs_most_important_policy_item_crime` | `hs_most_important_policy_keep_safe` |
| (and several others) | Discovered by querying `SELECT * LIMIT 1` |

**Requirements:** `DATABRICKS_API_KEY`, `DATABRICKS_SERVER_HOSTNAME`, `DATABRICKS_HTTP_PATH` env vars + `AWS_PROFILE=work`

**Collector:** `collectors/haystaq_voter.py`

### Charlotte Constituent Summary

| Metric | Value |
|--------|-------|
| Total registered voters | 642,783 |
| Voters with Haystaq scores | 611,378 |
| Zip codes covered | 29 |
| Average age | 46.0 |
| Democrat / Republican / Other | 266K / 109K / 268K |
| Ideology (liberal/conservative) | 64.0 / 36.0 |

**Top voter issues (Charlotte):**
1. Affordable Housing (Gov Role) — 66.4 (Strong)
2. Public Transit Support — 64.7 (Strong)
3. School Funding Support — 64.4 (Strong)
4. Climate Change Believer — 64.1 (Strong)
5. Anti-Gentrification Sentiment — 62.0 (Strong)

### Haystaq Expansion to Other States

Columns are consistent within a state. To add a new state:
1. Query which `hs_*` columns exist in that state's scores table
2. Pick the 12-18 most relevant columns
3. Write display names
4. This becomes the template for all cities in that state

Effort per state: 1-2 hours.

---

## Discussion Narratives (News Coverage)

Script 08 finds local news coverage for contentious legislative items to provide "what was discussed" context beyond the official record.

### Collection Method

1. **Gemini grounded web search** (`generate_with_search()`) — primary. Searches for each matter's title + city name, extracts narrative context.
2. **Tavily web search** — fallback when Gemini search fails. Uses domain filtering to focus on local news outlets.

### Source Verification

Every narrative includes:
- Source URLs for each claim
- Raw article excerpts (~2,000 chars) alongside URLs
- `is_directly_sourced` flag and `direct_quote` field for council member positions
- Programmatically calculated confidence scores (not LLM self-assessment)
- Only `high`/`medium` confidence items appear in the briefing

### Charlotte Results

| Metric | Value |
|--------|-------|
| Items researched | 20 |
| Items with coverage | 19 |
| Verified sources | 145 |
| Items excluded (low confidence) | 1 |

### Verifiability Risks

This is the **weakest data layer** in terms of verifiability:

1. **LLM may misrepresent sources** — paraphrase incorrectly, overstate positions, conflate articles
2. **Council member positions may be misattributed** — LLM inference from vote tallies, not direct reporting
3. **News articles are secondary sources** — journalists paraphrase and editorialize
4. **Not all items get news coverage** — items with 0 verified sources are excluded

**Mitigations:** Attribution-first language ("According to [Source]..."), confidence gating, disclaimer that context is from news coverage not official minutes.

**For production:** Replace web-searched news with official meeting minutes (from CodeLibrary/amlegal.com or Granicus video transcripts) for higher verifiability.

### News Outlet Configuration

Each city's `city_config.json` includes a list of local news domains (7-12 outlets) for focused search: major newspaper, TV affiliates, independent outlets, local newsletters, public radio.
