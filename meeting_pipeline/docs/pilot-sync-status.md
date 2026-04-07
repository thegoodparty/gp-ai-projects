# Pilot Sync Status

Tracks all 49 HubSpot officials: platform, data sync, normalization, Haystaq, and briefing status.

Last updated: 2026-04-06 (night)

**Status key:**
- ✅ done  |  ⏳ waiting on data  |  ❌ blocked  |  — not applicable

---

## Tier 1 — Ideal Platforms (Legistar / CivicClerk)

| Official | City, State | Platform | Meetings Synced | Normalized | Haystaq | Briefing | Notes |
|---|---|---|---|---|---|---|---|
| Nicole Shook | Johnstown, OH | civicclerk | ✅ Apr 7 (packet 8MB) | ✅ | ✅ 9,270 voters | ✅ | |
| AJ Ganim | Brecksville, OH | civicclerk | ✅ Mar 17 (packet 712KB) | ✅ | ✅ 27,537 voters | ✅ | Next meeting Apr 21 — no agenda posted yet |
| Mike Haigler | Locust, NC | civicclerk | ✅ Apr 9 (packet 231KB) | ✅ | ✅ 5,820 voters | ✅ | |
| Jay Davis | Texarkana, TX | civicclerk | ✅ Mar 9 (packet 36MB) | ✅ | ✅ 31,973 voters | ✅ | Apr 13 meeting in portal — packet not posted yet |
| Dan Reese | Windcrest, TX | civicclerk | ✅ Apr 6 (packet 6.4MB) | ✅ | ✅ 3,447 voters | ✅ | |
| Mickey Smith | Jacksonville, NC | civicclerk | ⏳ | — | — | — | **Data:** Collector works. Apr 21 regular meeting scheduled — no agenda posted yet. Was misidentified as Granicus in discovery (fixed). |
| Kim Singh | Mason, OH | civicclerk | ❌ | — | — | — | **Technical:** CivicClerk SPA — JS-only portal, our API collector returns no events. Needs Playwright. No future agendas visible. |
| Doug Weiss | Pflugerville, TX | legistar | ❌ | — | — | — | **Technical (city-side):** Legistar API returns 400 "Draft Status not setup" — city's Legistar is misconfigured. Portal confirmed correct (pflugerville.legistar.com). No future agendas posted yet. |

## Tier 2 — Good Platforms (CivicPlus / Granicus)

| Official | City, State | Platform | Meetings Synced | Normalized | Haystaq | Briefing | Notes |
|---|---|---|---|---|---|---|---|
| Guy Guidone | Louisville, OH | civicplus | ✅ Apr 6 (agenda 26KB) | ✅ | ✅ 13,411 voters | ✅ | Agenda only — site publishes no packets |
| Marcus Mcintyre | Indian Trail, NC | civicplus | ✅ Mar 10 (packet 15MB) | ✅ | ✅ 10,753 voters | ✅ | Apr agenda not yet posted |
| Candace Hunziker | Pittsboro, NC | civicplus | ✅ Apr 13 (packet 132MB) | ✅ | ✅ 20,127 voters | ✅ | |
| Kevin Edmonds | Dickinson, TX | civicplus | ✅ Mar 24 (packet 46MB) | ✅ | ✅ 23,487 voters | ✅ | Apr agenda not yet posted |
| Claudia Zapata | Kyle, TX | granicus | ✅ Apr 7 (packet 52MB) | ✅ | — | ✅ | Fixed: was misidentified as Novus. Packet from CloudFront CDN. Haystaq not yet run. |
| Arjenae Jones | Greenville, NC | granicus | ✅ Apr 9 (packet 18MB) | ✅ | ✅ 68,362 voters | ✅ | Packet from city CloudFront CDN — Granicus viewer only has agenda summary |
| Jess Hall | Lago Vista, TX | unknown_migrated | ❌ | — | — | — | **Technical + Data:** Migrated off CivicPlus AgendaCenter after April 2023. New platform unknown — landing page is static HTML, actual docs JS-rendered. No future agendas posted. |
| Matt Kadas | Hartville, OH | civicplus | ❌ | — | — | — | **Data:** City domain (hartvilleohio.com) is for sale on GoDaddy — no functioning city website exists. |
| Kristen Angelo | Walbridge, OH | civicplus | ❌ | — | — | — | **Data:** City website (villageofwalbridge.com) connection refused — site down. |
| Mark Huddleston | Mount Vernon, TX | municode | ❌ | — | — | — | **Technical:** Municode subdomain collector uses wrong endpoint (`/PublishPage/index` 404s). Portal uses Drupal `/views/ajax` — same bug affects Tomball TX. PDFs exist on Azure Blob (`mtvernontx-pubu`). Apr 13 meeting scheduled, packet not yet posted. |
| Michael Martinez | Sandy Oaks, TX | civicplus | ❌ | — | — | — | **Data:** City website (sandyoakstx.com) connection refused — site down. |

## Tier 3 — Marginal Platforms

| Official | City, State | Platform | Notes |
|---|---|---|---|
| Fred Ilarraza | Marvin, NC | escribemeetings | **Technical:** eSCRIBE collector ported to meeting_pipeline and runs successfully — connects to pub-marvinnc.escribemeetings.com. **Data:** 0 meetings returned in 180-day lookback — no agendas posted yet. Will collect automatically when posted. |
| Michael Benson | Lexington, OH | novus | **Technical:** No Novus Agenda collector built yet. |
| Mark Reams | Marysville, OH | municode_library | **Technical:** Municode Library (library.municode.com/oh/marysville/munidocs) is a JS SPA — different product from meetings.municode.com, our collector can't reach it. Needs Playwright or manual download. **Data:** Has 2026 packets (Mar 23 confirmed). No future agendas posted yet. |
| Todd Gordon | Lima, OH | civicplus | **Technical:** CivicPlus switched to JS-rendered "interactive agendas" module post-May 2024. Our scraper can't reach the new module. Needs Playwright. **Data:** No future agendas posted yet. |
| Patrick Shea | North Olmsted, OH | revize | **Data:** Apr 7 agenda + packet posted as direct PDFs (confirmed). **Technical:** Revize is a custom platform — no collector, skip per rule. |
| Christopher Gibbs | Palestine, TX | civicplus | **Data:** CivicPlus AgendaCenter Council category only has data through 2019 — likely moved to different category or stopped posting. Landing page at /324/ is static. No future agendas visible. Needs manual investigation. |
| Byron Bellman | Gibsonville, NC | generic | Re-run discovery |
| Mark Cozy | Canal Fulton, OH | unknown | Re-run discovery |
| Gregory Drew | Vermilion, OH | generic | Re-run discovery |
| Heather Basil | Mount Sterling, OH | generic | Re-run discovery |
| Brian Spitznagel | Walton Hills, OH | generic | Re-run discovery |
| Cody Mathews | Hillsboro, OH | none | Generic site with out-of-date agendas. No active meeting platform. |
| Abbie Bosak | Poland, OH | unknown | Re-run discovery |
| Laurie Mack | Salisbury, NC | unknown | Role says "Granite Quarry Town Council" — city/role mismatch, clarify |
| Jon Van De Riet | Stallings, NC | unknown | Re-run discovery |
| Ixtlazihuatl Vasquez | Refugio, TX | unknown | Re-run discovery |
| Edwina Agee | Maple Heights, OH | unknown | Re-run discovery |
| Berry Phillips | Coleman, TX | unknown | stale source — re-run discovery |
| Chad Deese | Pembroke, NC | unknown | Re-run discovery |

## Needs Discovery

| Official | City, State | Role | Notes |
|---|---|---|---|
| Linda O'Boyle | Elm, NC | Elm City Town Council | City not in sources — run discovery for "Elm City, NC" |

## Excluded — Not City Council

| Official | City, State | Role | Reason |
|---|---|---|---|
| Troy Holtrey | Clearcreek Township, OH | Warren County: Clearcreek Township Trustee | Township — no city council agendas |
| Rachel Zelazny | Etna Township, OH | Licking County: Etna Township Trustee | Township — no city council agendas |
| Bob Stone | Beavercreek, OH | Greene County: Beavercreek Township Trustee | Township — no city council agendas |
| David Mcintyre | Rootstown, OH | Portage County: Rootstown Township Trustee | Township — no city council agendas |
| Brian Valletto | Chardon, OH | Geauga County: Chardon Township Trustee | Township — no city council agendas |
| Tyler Scott | (no city), OH | Beavercreek City School Board | School board — not city council |
| Autum Barry | Logan, OH | School Board | School board — not city council |
| Vicki Smith | Van Wert, OH | Western Buckeye Educational Service Center | Education service — not city council |
| Daniel Stuckey | Pearland, TX | School Board | School board — not city council |
| Brian Roberson | Rosharon, TX | Alvin Independent School Board | School board — not city council |

---

## Summary

| Category | Count |
|---|---|
| Briefing done | 12 |
| Waiting on agenda posting (collector works) | 5 |
| Blocked — technical (our pipeline) | 4 |
| Blocked — technical (city-side) | 1 |
| Blocked — data (site down / no platform) | 4 |
| Marginal — needs discovery or Playwright | 13 |
| Needs discovery | 1 |
| Excluded (township/school) | 10 |
| **Total** | **50** |

## Pipeline Issues Found

| Issue | Affected Cities | Fix Applied |
|---|---|---|
| `find_best_pdf` picked agenda over packet | All CivicClerk cities | ✅ Fixed — now sorts by "packet" in name then largest size |
| CivicPlus filenames use `YYYYMMDD` not `YYYY-MM-DD` | Indian Trail, Dickinson, Pittsboro, Louisville | ✅ Fixed — glob now checks both date formats |
| `agenda_posted_no_files` entries skipped even when PDF on disk | Greenville NC | ✅ Fixed — extraction now checks for local PDF on these entries |
| Gemini truncates structured JSON on large agendas (30+ items) | Texarkana TX (31 items) | ✅ Fixed — large agendas use shorter descriptions; retry loop added |
| Granicus scraper captures viewer URLs, not direct PDF links | Kyle TX, Greenville NC | ⚠️ Workaround — packet URL found manually from page source and downloaded directly |
| Municode subdomain collector uses wrong API endpoint | Mount Vernon TX, Tomball TX | ❌ Not fixed — collector hits `/PublishPage/index` (404); portal uses Drupal `/views/ajax` |
| CivicClerk SPA blocks API collector | Mason OH | ❌ Not fixed — needs Playwright |
| Legistar API blocked by city misconfiguration | Pflugerville TX | ❌ Not fixable on our end — city must reconfigure Legistar |
| Municode Library is a different JS product | Marysville OH | ❌ Not fixed — needs Playwright or manual download |
| CivicPlus "interactive agendas" JS module not scrapeable | Lima OH | ❌ Not fixed — needs Playwright |
