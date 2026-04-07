# City Collection Notes — Per-City Status & Nuances

**Updated:** 2026-03-31
**Purpose:** Current state of data collection for each city, including platform quirks and workarounds.

---

## Tier 1 — Legistar API (10 cities, all collected)

Structured API at `webapi.legistar.com/v1/{slug}/events`. Collection via `collect_legistar_batch.py` + `transform_to_meeting_data.py`.

| City | State | Slug | Events | Notes |
|------|-------|------|--------|-------|
| Austin | TX | `austintexas` | 31 | ✅ Collected + transformed. Large council with many boards. |
| Chapel Hill | NC | `chapelhill` | 18 | ✅ Collected + transformed. Clean data, 41 PDFs. Uses "Town Council" not "City Council". |
| Cleveland | OH | `cityofcleveland` | 57 | ✅ Collected + transformed. Very active council with committee meetings. |
| Dallas | TX | `cityofdallas` | 154 | ✅ Collected + transformed. 852 matters, 991 PDFs. Some attachment URLs are safelinks-wrapped and return 302. |
| El Paso | TX | `elpasotexas` | 95 | ✅ Collected + transformed. 540 matters, 502 PDFs. |
| Killeen | TX | `killeen` | 11 | ✅ Collected + transformed. **TRAP:** Discovery pointed to CivicPlus, but CivicPlus only has advisory boards (Parks & Rec, Board of Adjustment, Charter Review). City Council is on Legistar. `boardbook.org/Organization/1051` is Killeen ISD (school board). source.json corrected. |
| Farmers Branch | TX | `farmersbranch` | 47 | ✅ Collected + transformed. 270 matters, 190 PDFs. |
| Fayetteville | NC | `cityoffayetteville` | 12 | ✅ Collected + transformed. 124 matters, 356 PDFs. |
| Lexington | NC | `lexingtonnc` | 5 | ⚠️ **WRONG BODY + STALE.** Collected 5 Planning Board/Board of Adjustment events (body 194). City Council body (id=138) exists but is **stale on Legistar** — most recent event Dec 22, 2025 with no agenda PDF. CivicPlus at `lexingtonnc.gov/AgendaCenter` returns HTTP 403. `cityoflex.com/meeting-agendas-minutes/` also 403. All known sources blocked or stale. Wrong meetings.json deleted. Effectively Tier 4 — needs investigation to find if city moved to a new website. |
| New Braunfels | TX | `newbraunfels` | 85 | ✅ Collected + transformed. 300 matters, 257 PDFs. |

**Total: ~515 events across 10 cities. 9 transformed to meetings.json (Lexington excluded — wrong body).**

### Data verification (6 reference cities)

Verified 2026-04-04 that data is correct:

**Easy (high confidence):**
- **Cleveland OH**: 9 City Council meetings Jan 5 → Mar 30. Detailed agenda items (appointments, ordinances, license transfers). Excellent.
- **Chapel Hill NC**: 10 Town Council meetings Jan 7 → Mar 25. Rich items (housing bonds, arts grants, budget appropriations). Has vote results + staff recommendations.
- **Dallas TX**: 6 City Council meetings Jan 14 → Mar 25 (biweekly). Outstanding detail — numbered items, fiscal amounts ($4.37M, $660K), consent agenda, public hearings, zoning.

**Tricky (platform mismatch confirmed):**
- **Killeen TX**: Legistar data correct (City Council, BodyId 245). CivicPlus data was wrong (Boards & Commissions only). Wrong data deleted.
- **Fairfield OH**: CivicClerk data correct (City Council, 16 events). CivicPlus data was wrong (Board of Zoning Appeals only). Wrong data deleted.
- **Duncanville TX**: CivicClerk data correct (City Council, 3 events). BoardBook was wrong (school district ISD). source.json corrected.

---

## Tier 1 — CivicClerk OData API (9 cities)

API at `{tenant}.api.civicclerk.com/v1/Events/`. Collection via `collect_civicclerk_batch.py`.

| City | State | Tenant | Events | Agenda PDFs | Notes |
|------|-------|--------|--------|-------------|-------|
| Duncanville | TX | `duncanvilletx` | 5 | 5 | ✅ Collected. **TRAP:** Discovery pointed to BoardBook (Duncanville ISD = school district). City Council is here. source.json corrected. Most recent: 2026-03-17. |
| Fairfield | OH | `fairfieldoh` | 15 | 2 | ✅ Collected. **TRAP:** Discovery pointed to CivicPlus (Board of Zoning Appeals only). City Council is here. source.json corrected. |
| Huntersville | NC | `huntersvillenc` | 4 | 0 | Uses "Town Board" instead of "City Council". Pre-scheduled future meetings, no agendas yet. Novus Agenda (EOL) also exists at `huntersville.novusagenda.com`. |
| Midland | TX | `midlandtx` | 3 | 3 | ✅ Collected. Discovery and prior registry both pointed to CivicPlus — but CivicPlus City Council data stale since 2020. CivicClerk is the live source. Most recent: 2026-03-31. |
| Perrysburg | OH | `perrysburgoh` | 4 | 4 | ✅ Collected. Previously appeared as 0 PDFs (only future pre-scheduled meetings returned). Fixed by two-query strategy. Most recent: 2026-03-17. |
| Sherman | TX | `shermantx` | 7 | 7 | ✅ Collected. Previously appeared empty (`publishedFiles` empty). Fixed by two-query strategy + lowercase `eventDate` fix. Most recent: 2026-04-06. |
| Texarkana | TX | `texarkanatx` | 2 | 2 | ✅ Collected. |

**Two critical fixes applied to `collectors/civicclerk.py`:**
1. **Case-sensitivity bug:** OData field names are case-sensitive. `EventDate` (capital E) returns only future pre-scheduled meetings; `eventDate` (lowercase) returns past meetings correctly. All parameter values changed to `eventDate`.
2. **~15-event page cap:** Despite `$top=500`, the API returns at most ~15 events per request. Fixed by splitting into two queries — `(cutoff→today)` for past events and `(today→future+60d)` for upcoming — and deduplicating by event ID.

**Nuance:** CivicClerk pre-schedules future meetings far in advance. `hasAgenda=True` doesn't mean the PDF is available — some tenants don't expose files via the public OData API.

---

## Tier 2 — CivicPlus AgendaCenter (7 active cities)

Scraper at `collectors/civicplus_scraper.py`. Collection via `collect_civicplus_batch.py`. LLM extraction via `extract_meeting_from_pdf.py`.

| City | State | Domain | PDFs | Extracted | Notes |
|------|-------|--------|------|-----------|-------|
| Canal Winchester | OH | `canalwinchesterohio.gov` | 7 | 7 | ✅ Done. Checkbox labels don't match AJAX content — scraper verifies via AJAX `aria-label`. |
| Centerville | OH | `centervilleohio.gov` | 13 | 13 | ✅ Done. Same checkbox mismatch pattern. |
| Longview | TX | `longviewtexas.gov` | 6 | 6 | ✅ Done. |
| North Canton | OH | `northcantonohio.gov` | 10 | 9 | ✅ Done. Gemini hallucinated many dollar amounts on budget PDFs. One PDF failed all 3 retries. |
| Troy | OH | `troyohio.gov` | 7 | 4 | ✅ Done. 2 of 7 are scanned. |
| Durham | NC | `durhamnc.gov` | 13 | 0 | ❌ All scanned PDFs — need OCR. |
| Parma | OH | `cityofparma-oh.gov` | 6 | 0 | ❌ All scanned PDFs — need OCR. |

**Not collecting from CivicPlus (wrong source):**
- **Concord NC**: CivicPlus only has advisory committees. City Council is at `concordnc.portal.civicclerk.com` (SPA, OData returns 404 — needs Playwright).
- **Fairfield OH**: CivicPlus only has Board of Zoning Appeals. → Use CivicClerk.
- **Killeen TX**: CivicPlus only has advisory boards. → Use Legistar.
- **Midland TX**: CivicPlus data stale since 2020 for City Council. → Use CivicClerk.
- **Rocky Mount NC**: CivicPlus City Council stale since 2024. Use generic HTML scraper on `/497/Council-Agendas-Minutes` instead.

---

## Tier 2 — Granicus/Swagit (7 cities) — Classic Granicus working, New Swagit needs fix

Scraper at `collectors/granicus_scraper.py`. Batch at `scripts/collect_granicus_batch.py`. Two variants: classic Granicus (RSS feeds at `ViewPublisherRSS.php`) and new Swagit (JSON API at `/{body-slug}.json`).

### Classic Granicus — 4 cities, 3 collected

| City | State | Subdomain | view_id | Events | PDFs | Notes |
|------|-------|-----------|---------|--------|------|-------|
| Gastonia | NC | `cityofgastonia` | 1 | 6 | 3 | ✅ Collected. |
| Greenville | NC | `greenville` | 10 | 12 | 11 | ✅ Collected. **Moved from Tier 4.** CivicPlus stale (advisory only). Required SSL bypass fix for Granicus S3 bucket. |
| Powell | OH | `cityofpowell` | 2 | 10 | 0 | ✅ Events collected, no agenda PDFs available in RSS. |
| Cibolo | TX | `cibolotx` | 1 | 0 | 0 | ❌ RSS feed returned 0 items within 90-day lookback. May need different view_id. |

### New Swagit — 3 cities, scraper broken

| City | State | Subdomain | Status | Notes |
|------|-------|-----------|--------|-------|
| Beaumont | TX | `beaumonttx` | ❌ Scraper bug | `city-council.json` returns list of `{id, title, url}` pointers — scraper treated them as events instead of following the URL. Needs per-event JSON fetch. |
| Fairborn | OH | `fairbornoh` | ❌ Scraper bug | `city-council.json` returns empty `[]`. `/events.json` is a **global Swagit feed** (25K+ events from all tenants), not city-specific. City data is in the JS-rendered SPA only. |
| Kyle | TX | `kyletx` | ❌ Scraper bug | Same as Fairborn. `city-council.json` empty. `/events.json` is global. SPA has data. |

**Granicus scraper fixes applied:**
1. **SSL bypass**: Granicus S3 bucket (`granicus_production_attachments.s3.amazonaws.com`) has certificate hostname mismatch. Added fallback that retries PDF downloads with `verify=False` when SSL errors occur.
2. **Swagit --no-pdfs speedup**: Skipped per-event `_get_swagit_pdf_url` API calls when `--no-pdfs` flag is set.

**Unresolved: New Swagit API structure.** The scraper assumes `city-council.json` returns event objects directly. But for Beaumont, it returns a list of pointers (`{id, title, url}`) where each `url` leads to per-event JSON. For Fairborn/Kyle, the endpoint is empty and the page is a JS SPA. The 3 New Swagit cities need either: (a) fix scraper to follow the Beaumont-style pointer URLs, or (b) use Playwright for the SPA.

---

## Tier 2 — Municode (3 cities) — Not built

| City | State | URL | Notes |
|------|-------|-----|-------|
| Apex | NC | `meetings.municode.com/...?cid=APEXNC` | Standard Municode meeting portal. |
| Grand Prairie | TX | `meetings.municode.com/...?cid=GPTX` | Standard Municode. |
| Tomball | TX | `tomball-tx.municodemeetings.com` | Different subdomain format. |

---

## Tier 2 — Other Platforms (2 cities)

| City | State | Platform | Notes |
|------|-------|----------|-------|
| Cleburne | TX | Diligent | `cleburne.community.diligentoneplatform.com`. Need to investigate scraping. |
| Huntersville | NC | Novus Agenda / CivicClerk | Novus Agenda at `huntersville.novusagenda.com` is EOL. CivicClerk OData at `huntersvillenc.api.civicclerk.com` works and is in Tier 1 above. |

---

## Tier 3 — Generic HTML Scraper (18 cities in registry)

Scraper at `collectors/generic_html_scraper.py`. Batch at `scripts/collect_generic_batch.py`. Per-city config registry with 5 extraction strategies.

### Easy tier — run, 3 of 5 successful

| City | State | Strategy | Meetings | PDFs | Status |
|------|-------|----------|----------|------|--------|
| Dublin | OH | direct_pdf | 14 | 14 | ✅ Collected — most recent 2026-04-02. Needs LLM extraction. |
| Cuyahoga Falls | OH | two_hop | 23 | 23 | ✅ Collected — most recent council meeting 2026-01-26. Two-hop Drupal strategy. Needs LLM extraction. |
| Rocky Mount | NC | document_center | 15 | 15 | ✅ Collected — most recent 2026-03-23. CivicPlus `/DocumentCenter/View/{id}` pattern. Needs LLM extraction. |
| New Bern | NC | direct_pdf | 18 | 0 | ❌ Revize CMS CDN 404s. `?t=` query param double-applied in redirect. Needs manual fix. |
| Temple | TX | direct_pdf | 5 | 0 | ❌ Same Revize CMS CDN issue. Needs manual fix. |

### Medium tier — run, 3 of 5 successful

| City | State | Strategy | Meetings | PDFs | Status |
|------|-------|----------|----------|------|--------|
| Burlington | NC | archive_aspx | 11 | 11 | ✅ Collected — most recent 2026-03-17. CivicPlus Archive.aspx ADID links work. Needs LLM extraction. |
| Salisbury | NC | direct_pdf | 21 | 21 | ✅ Collected — most recent 2026-03-17. DNN LinkClick.aspx works. Needs LLM extraction. |
| Warren | OH | direct_pdf | 61 | 61 | ✅ Collected — most recent 2026-03-24. `cityofwarren.org` URL works (earlier attempt on `warren.org` failed). Needs LLM extraction. |
| Hamilton | OH | direct_pdf | 51 | 51 | ⚠️ **Wrong body.** Squarespace page pulls docs from all departments — Planning Commission, Finance, etc. not just City Council. Needs URL-level filtering. |
| Lima | OH | document_center | 1 | 1 | ⚠️ Only 1 stale PDF (2024) found on landing page. PrimeGov portal (active since May 2024) refused connection. Needs investigation. |

### Hard tier — run, all 6 failed as expected

| City | State | Strategy | Notes |
|------|-------|----------|-------|
| Jacksonville | NC | direct_pdf | ❌ Calendar-based; requires JS event navigation. |
| Lancaster | TX | archive_aspx | ❌ Dropdown form; requires POST with AMID/ADID. |
| Mason | OH | direct_pdf | ❌ WordPress calendar plugin. Events link to JS-rendered detail pages. |
| Matthews | NC | direct_pdf | ❌ Granicus/Telerik ASP.NET. |
| Monroe | NC | archive_aspx | ❌ ASP.NET ViewState + dropdown form, AJAX postback. |
| Statesville | NC | direct_pdf | ❌ Returns 403 to all automated requests. Needs headless browser. |

### Ad-hoc Phase 3 collections — former Tier 4 cities unblocked

Cities that were previously listed as Tier 4 (CivicClerk SPA / inaccessible) and are now collected via alternative methods:

| City | State | Method | PDFs | Most Recent | Status |
|------|-------|--------|------|-------------|--------|
| Belton | TX | Generic HTML scrape (`beltontexas.gov`) | 6 | 2026-03-24 City Council | ✅ Collected. CivicClerk portal (`beltontx`) has no OData API. City website (Revize CMS) has direct PDF links. Filenames use MMDDYY format (e.g., `032426` → 2026-03-24). Script: `collect_belton_tx.py`. |
| Euclid | OH | eGovLink REST API | 6 | 2026-03-02 EUCLID CITY COUNCIL | ✅ Collected. City website is Duda CMS (fully JS-rendered), but the embedded document widget uses eGovLink API (`apidocprod.egovlink.com/documents/`). JWT token and folder IDs extracted from Base64-encoded `data-widget-config` attribute in page HTML. Folder path: Meetings(39372)→Council(39411)→Final Agendas(52849)→2026(52851). Script: `collect_euclid_oh.py`. |
| Hickory | NC | Generic HTML scrape (`hickorync.gov`) | 6 (new) | 2026-03-17 Hickory City Council | ✅ Collected. CivicClerk portal (`hickorync`) has no OData API. Drupal CMS page (`/agendas-and-minutes`) has direct PDF links. Filenames use YYYYMMDD prefix. Skips "Action Agenda" files. **SSL cert invalid** — scraper uses `verify=False`. Script: `collect_hickory_nc.py`. 30 meetings extracted total (includes historical). |

### Ad-hoc Phase 2 collections (research + manual download)

Cities that needed targeted research outside the batch runner:

| City | State | Method | PDFs | Most Recent | Status |
|------|-------|--------|------|-------------|--------|
| Asheville | NC | Google Docs export-as-PDF | 6 | 2026-03-24 | ✅ Collected. Generic scraper failed (links go to Google Docs, not `.pdf` hrefs). Workaround: extract the Google Doc URL from the agenda page and append `/export?format=pdf`. Source: `ashevillenc.gov/government/city-council-meeting-materials/`. |
| Stow | OH | Granicus portal | 6 | 2026-03-12 | ✅ Collected. Generic scraper failed on landing page (`stowohio.gov/510/Public-Meeting-Information`) — no PDFs there. Real source is Granicus: `stowohio.granicus.com/ViewPublisher.php?view_id=3`. PDFs served from CloudFront CDN. |
| Clayton | NC | CivicClerk API | 4 | 2026-03-02 | ✅ Collected. Source redirects to `claytonnc.portal.civicclerk.com`. OData API at `claytonnc.api.civicclerk.com/v1/Events` works (unlike other CivicClerk SPA cities). Agenda PDFs in Azure Blob Storage. Town Council body. |
| Marysville | OH | Swagit PDFs (pre-existing) | 5 | 2026-03-23 | ✅ Collected. PDFs already present in `sources/marysville-oh/`. CivicPlus Archive.aspx at the configured URL was returning BZA meetings — Swagit data is the correct City Council source. |
| Medina | OH | WordPress direct PDF uploads | 6 | 2026-03-23 | ✅ Collected. Source: `medinaoh.org/city-hall/city-council/clerk-of-council/2026-council-finance-packets/`. Direct WordPress media uploads, no JS needed. Note: BoardDocs at `boarddocs.com` is Medina City Schools (school district), not the city. |
| Lufkin | TX | Revize CMS w/ agenda PDFs | 4 | 2026-03-17 | ✅ Collected. Previously listed in Tier 4 as "webcast only." Re-investigation found that `cityoflufkin.com/government/council_webcasts.php` actually has agenda PDFs alongside webcasts on Revize CMS. Generic scraper CDN issue may or may not affect this — collected manually. |
| La Porte | TX | — | 0 | — | ❌ In transition. CivicPlus archive stops at Feb 2024. New system is fully JS-rendered ("Upcoming/Past Meetings" sections render via JS). No static API found. Needs Playwright or manual. |
| Loveland | OH | — | 0 | — | ❌ Uses Diligent One Platform (`cleburne.community.diligentoneplatform.com`-style). Fully JS-rendered. No static API or RSS found. Same issue as Cleburne TX. |
| Palestine | TX | — | 0 | — | ❌ **No automated source.** CivicPlus AgendaCenter last City Council entry is 2019. `cityofpalestinetx.com/AgendaCenter` returns 200 but has no parseable meeting data. `cityofpalestinetx.com/324/Meeting-Agendas-and-Minutes` is informational only. May be Facebook-only. Mark as no-automated-source. |

### Generic HTML scraper fixes applied

1. **PDF detection with query params**: Changed from `href.lower().endswith(".pdf")` to `urlparse(href).path.lower().endswith(".pdf")` to strip `?t=` cache-busting params (Revize CMS).
2. **Space encoding**: Added `href.replace(" ", "%20")` for Revize CMS URLs with spaces.
3. **Two-hop strategy**: New strategy for Drupal sites (Cuyahoga Falls) where index page links to subpages, subpages have PDFs.

### Unresolved: Revize CMS CDN 404s

New Bern NC and Temple TX use Revize CMS. PDF links are detected correctly after the query param fix, but downloads fail because:
- City domain (e.g., `newbernnc.gov`) redirects to CDN (`cms7files.revize.com`)
- The redirect URL gets a double `?t=` parameter, causing 404

---

## Tier 4 — Fragile/Inaccessible

| City | State | Issue | Status / Workaround |
|------|-------|-------|---------------------|
| Belton | TX | ~~CivicClerk SPA, OData API returns 404~~ | ✅ **Resolved** — HTML scrape of `beltontexas.gov`. See Phase 3 ad-hoc. |
| Concord | NC | CivicClerk SPA, OData API returns 404. **Wrong body in existing extraction** — data is Bicycle and Pedestrian Advisory Committee, not City Council. | Needs Playwright to re-collect correct body. CivicPlus only has advisory committees. |
| Delaware | OH | Stale since 2021, JS-rendered page | Manual only |
| Euclid | OH | ~~CivicClerk SPA, OData API returns 404~~ | ✅ **Resolved** — eGovLink REST API. See Phase 3 ad-hoc. |
| Greensboro | NC | eSCRIBE blocks all automation (403, SSL errors) | No accessible fallback source |
| Hickory | NC | ~~CivicClerk SPA, OData API returns 404~~ | ✅ **Resolved** — HTML scrape of `hickorync.gov`. See Phase 3 ad-hoc. |
| Jacksonville | NC | Calendar-based site, JS event navigation required | Hard-tier generic scraper failed. Needs Playwright. |
| La Porte | TX | Mid-migration. CivicPlus archive stops Feb 2024. New system is JS-rendered. | Needs Playwright or manual |
| Lancaster | TX | Dropdown form, requires POST (AMID/ADID) | Hard-tier generic scraper failed. Needs Playwright. |
| Lexington | NC | All sources blocked or stale. Legistar City Council body stale (Dec 2025). CivicPlus 403. `cityoflex.com` 403. | Needs investigation — city may have moved to new website entirely. |
| Loveland | OH | Diligent One Platform, fully JS-rendered | Same issue as Cleburne TX. Needs Playwright. |
| Lufkin | TX | **Resolved** — was "webcast only" but agendas found on same Revize CMS page | ✅ Moved to Phase 2 ad-hoc. Collected 4 PDFs, most recent 2026-03-17. |
| Mason | OH | WordPress calendar, events link to JS-rendered detail pages | Hard-tier generic scraper failed. Needs Playwright. |
| Matthews | NC | Granicus/Telerik ASP.NET | Hard-tier generic scraper failed. Needs Playwright. |
| Monroe | NC | ASP.NET ViewState + dropdown, AJAX postback | Hard-tier generic scraper failed. Needs Playwright. |
| Palestine | TX | **No automated source.** CivicPlus last entry 2019. All other URLs informational only. | Mark as no-automated-source. |
| Statesville | NC | Returns 403 to all automated requests | Needs headless browser / Playwright. |
| Westerville | OH | All automated approaches exhausted | ❌ **Hard blocked.** (1) `westervilleoh.api.civicclerk.com/v1/Events` → HTTP 404 — no public OData API. (2) CivicClerk portal (`westervilleoh.portal.civicclerk.com`) requires OAuth2 login before loading meeting data — Playwright interception captured only static asset requests, no event API calls. (3) `westerville.org/AgendaCenter` → HTTP 403 on all paths, including Playwright with full browser headers. Manual download from authenticated CivicClerk session is the only known path. |

**CivicClerk SPA cities still unresolved (2):** Concord NC and Westerville OH. Belton TX, Euclid OH, and Hickory NC were previously in this list but have been collected via alternative methods.

**Note on Greenville NC:** Was incorrectly listed here in earlier versions. Moved to Tier 2 Granicus — see that section.
**Note on Lufkin TX:** Was listed as "webcast only" — incorrect. Agenda PDFs found on Revize CMS page. Moved to Phase 2 ad-hoc collected.
**Note on Palestine TX:** Source.json incorrectly pointed to Facebook in earlier discovery. Re-investigated: CivicPlus AgendaCenter last City Council entry is 2019. No working digital source found.
**Note on Concord NC:** Existing extraction contains wrong body (Bicycle and Pedestrian Advisory Committee). Must re-collect via Playwright targeting City Council body specifically.

---

## Platform Mismatch Traps — Lessons Learned

Three cities had their `source.json` pointing to the wrong platform because the discovery algorithm picks the "freshest" source without verifying it's City Council content:

1. **Killeen TX**: CivicPlus AgendaCenter has Boards & Commissions (Parks & Rec, Board of Adjustment, Charter Review). These post regularly, so the page looks "fresh." But there's zero City Council data. Real council is on Legistar.

2. **Fairfield OH**: CivicPlus AgendaCenter has only Board of Zoning Appeals (3 meetings). Real City Council is on CivicClerk (16 meetings).

3. **Duncanville TX**: BoardBook Organization 858 is Duncanville Independent School District. The notes from Tavily even say "Duncanville ISD Public View" but the algorithm ranked it #1 because it was "fresh." Real City Council is on CivicClerk.

**What the discovery algorithm should add:** After finding the "best source," verify the meeting body name contains "Council" (or "Board of Aldermen" for New Bern, "Town Board" for Huntersville, etc.). If it only has advisory/zoning/school content, demote it and look for the next candidate.

**Impact on the skills pipeline:** The `briefing-collect` and `briefing-score` skills also lack body-type validation. The scoring rubric's 12 dimensions don't include "correct legislative body." A briefing generated from Board of Zoning Appeals data would score "review" (not "hold") — low quality but not flagged as fundamentally wrong.
