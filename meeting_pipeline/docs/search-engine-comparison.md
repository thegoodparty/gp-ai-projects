# Search Engine Comparison: Finding City Council Agenda URLs

**Context:** For the municipal meeting data pipeline, we need to reliably find the official city council agenda page for 201 cities across the US. We tested four search backends and compared their top result for each city.

**What "correct" means here:** The result points to the right city and body, and is a page the pipeline can actually collect data from. A Legistar, CivicClerk, or Municode URL is just as correct as the city's own website — those are the platforms we use to pull structured agenda data. What's wrong is a different city, a different body (county instead of city), a completely irrelevant site, or an inaccessible raw API endpoint.

**Initial benchmark methodology:** We used Serper.dev (real Google results) as ground truth and marked any result with a different domain as incorrect. This overstated failure rates — particularly for Gemini Grounding, which often found valid platform URLs (CivicClerk, Legistar, Municode) that we penalized simply because they had a different domain than what Google returned. The corrected analysis is below.

---

## Summary

| Engine | Domain match vs Serper | Corrected accuracy (right city/body/collectible) |
|---|---|---|
| **SerpApi** | 200/201 (100%) | **~99%** |
| **Serper.dev** | 201/201 (100%) | **~99%** |
| **Exa** | 179/201 (89%) | **89%** |
| **Gemini Grounding** | 143/201 (71%) | **94%** (189/201) |
| **Tavily** | — | dev key quota exhausted |

Grounding's 71% domain-match rate was misleading — 45 of its 58 apparent "failures" were finding a valid CivicClerk, Legistar, or Municode portal instead of the city's main website, which are equally correct for pipeline purposes. The real failure count is 12 cities, driven by two specific patterns.

---

## Gemini Grounding: Real Failures vs. False Flags

### Actually Wrong: Different City or Body

These are genuine errors — the result points to a completely different city or a non-city entity.

| City | Serper (correct) | Grounding returned | Problem |
|---|---|---|---|
| Clear Lake, **IA** | `cityofclearlake.com/agenda.aspx` | `clearlake-ca.municodemeetings.com` | **California city**, not Iowa |
| Chisago, MN | `ci.chisago.mn.us/agendasminutes` | `chisagocountymn.gov/AgendaCenter` | **County** government, not city |
| Ocean, NJ | `ocnj.us/meetings` | `theoceancountylibrary.org` | **The county library** |

These are the genuinely bad results — wrong entity, wrong data.

### Actually Wrong: Raw API Endpoints

For several Legistar cities, grounding returned the internal REST API JSON endpoint rather than any human-facing or pipeline-usable page. While the Legistar API is useful, this specific URL pattern (`webapi.legistar.com/v1/{tenant}/events?$top=3&...`) returns a 3-event JSON snippet — not the full event listing the pipeline needs.

| City | Serper (correct) | Grounding returned |
|---|---|---|
| North Port, FL | `northportfl.gov/City-Government/Meetings` | `webapi.legistar.com/v1/cityofnorthport/events?$top=3&$orderby=EventDate+desc` |
| Pompano Beach, FL | `pompanobeachfl.gov/meetings` | `webapi.legistar.com/v1/pompano/events?$top=3&$orderby=EventDate+desc` |
| Chapel Hill, NC | `chapelhillnc.gov/Town-Government/Meetings-and-Agendas` | `webapi.legistar.com/v1/chapelhill/events?$top=3&$orderby=EventDate+desc` |
| Rochester, MN | `rochestermn.gov/council-administration/meetings/` | `webapi.legistar.com/v1/cityofrochester/events?$top=3&$orderby=EventDate+desc` |
| Milwaukee, WI | `city.milwaukee.gov/cityclerk/PublicRecords/Agendas.htm` | `webapi.legistar.com/v1/milwaukee/events?$top=3&$orderby=EventDate+desc` |

### Not Actually Wrong: Valid Platform Portals

These were counted as failures in the initial benchmark but are not. Both URLs point to the correct city's official agenda data on a platform we already collect from.

| City | Serper | Grounding | Notes |
|---|---|---|---|
| Baldwin Park, CA | `baldwinpark.com/agendacenter` | `baldwinparkca.portal.civicclerk.com` | Both correct — CivicClerk is the official portal |
| Brookline, MA | `brooklinema.gov/agendas` | `brooklinema.portal.civicclerk.com` | Both correct |
| Kalamazoo, MI | `kalamazoocity.org/.../Minutes-Agendas` | `kalamazoomi.portal.civicclerk.com` | Both correct |
| Texarkana, TX | `texarkanatexas.gov/AgendaCenter` | `texarkanatx.portal.civicclerk.com` | Both correct |
| Blaine, WA | `ci.blaine.wa.us/276/City-Council-Agenda` | `blainewa.portal.civicclerk.com` | Both correct |
| Pewaukee, WI | `cityofpewaukee.us/514/Agendas-and-Minutes` | `pewaukeewi.portal.civicclerk.com` | Both correct |
| Austell, GA | `austellga.gov/AgendasandMinutes.aspx` | `austell-ga.municodemeetings.com` | Both correct — Municode is the official portal |

In fact, grounding finding the CivicClerk or Municode portal directly is often *better* for the pipeline than the city's own website, since those platforms expose structured, machine-readable data.

---

## Exa: Real Failures (~22 cities)

Exa's failures are more consistently wrong because it optimizes for relevant *content* rather than authoritative *destinations*. It returns news articles, blog posts, and wrong-state cities with similar names — none of which yield agenda data.

### News Articles Instead of the City's Agenda Page

| City | Serper (correct) | Exa returned |
|---|---|---|
| Evanston, IL | `cityofevanston.org/.../agendas_minutes` | `evanstonroundtable.com/...evanston-city-council-strategic-housing-plan-tabled/` |
| Chapel Hill, NC | `chapelhillnc.gov/Town-Government/Meetings-and-Agendas` | `dailytarheel.com/article/city-chapel-hill-council-mayor-changes-20260424` |
| Norfolk, NE | `norfolkne.gov/.../agenda-minutes-and-videos/` | `norfolkdailynews.com/news/agenda-for-upcoming-city-council-meeting/...` |
| Petoskey, MI | `petoskey.us/.../agendas___minutes.php` | `petoskeynews.com/.../petoskey-council-discusses-proposed-towing-ordinance/...` |
| Reno, NV | `reno.gov/government/city-council` | `mynews4.com/news/local/reno-city-council-advances-data-center-regulations-ai...` |
| Hermiston, OR | `hermiston.gov/meetings1` | `eastoregonian.com/.../hermiston-councilor-linton-wanted-100000-plus-salaries/...` |

Exa is finding highly relevant pages *about* council meetings — local newspaper coverage of the same meetings we want to track. Useful for a different purpose, but not a source of structured agenda data.

### Wrong City (Semantically Similar Name)

| City | Serper (correct) | Exa returned |
|---|---|---|
| California City, CA | `californiacity-ca.gov/.../city-clerk` | `communityforwardredlands.com/redlands-city-council-agenda-april-7-2026/` ← **Redlands, CA** |
| Williamstown, MA | `williamstownma.gov/public-meeting-minutes/` | `wtownky.org/government/agendas___minutes.php` ← **Williamstown, KY** |
| Camden, NJ | `camdennj.gov/council-agendas/` | `camdenarknews.com/.../camden-city-council-declares-several-structures/` ← **Camden, AR** |

---

## Where All Engines Agreed

130 of 201 cities (65%) had identical results across all engines — cities with a single clearly authoritative page that every search backend returns first.

| City | URL |
|---|---|
| Tuscaloosa, AL | `tuscaloosa.com/meetings` |
| Tuskegee, AL | `tuskegeealabama.gov/node/43/agenda` |
| Fort Smith, AR | `fortsmithar.gov/government/meetings-agendas` |
| Rogers, AR | `rogersar.gov/632/Agendas-Minutes` |
| Surprise, AZ | `surpriseaz.gov/734/Meetings-Agendas-Minutes` |
| La Habra, CA | `lahabraca.gov/149/Council-Meetings` |
| Loveland, CO | `lovgov.org/city-government/city-council/city-council-meetings` |
| Thornton, CO | `thorntonco.gov/government/mayor-council/upcoming-meetings` |
| Lynn Haven, FL | `cityoflynnhaven.com/AgendaCenter` |
| Stockbridge, GA | `stockbridgega.portal.civicclerk.com` |
| Pocatello, ID | `pocatello.gov/AgendaCenter/City-Council-4` |
| Champaign, IL | `champaignil.gov/council/` |
| Methuen, MA | `methuen.gov/AgendaCenter/City-Council-26/` |
| Brunswick, ME | `brunswickme.gov/AgendaCenter/Town-Council-13` |
| Westland, MI | `cityofwestland.com/AgendaCenter` |
| Rochester, MN | `rochestermn.gov/council-administration/meetings/` |
| Ballwin, MO | `ballwin-mo.municodemeetings.com/` |
| Racine, WI | `cityofracine.legistar.com/MainBody.aspx` |

---

## Conclusion

**Serper.dev and SerpApi are the right choice** for this use case. They return real Google Search results as a US user would see them — navigational, geographically grounded, and pointing to the most authoritative source for each city.

- **Gemini Grounding** has a real failure rate of 12/201 cities (6%), not 29% as the domain-match metric suggested. Its actual failures are wrong-city/wrong-body results and raw `webapi.legistar.com` API endpoints. Its apparent "failures" on CivicClerk/Legistar/Municode portals are not failures at all — those are valid, often preferable data sources for structured collection.
- **Exa** has a consistent ~22-city failure rate driven by returning news content instead of agenda pages. It's useful for content research but not for navigational source discovery.
- **Serper.dev** costs ~$1/1,000 queries vs $15/1,000 for SerpApi and returns equivalent results, making it the right production choice.

The key lesson from the initial benchmark methodology: **domain matching is the wrong metric**. What matters is whether the result points to the right city and body, and whether the pipeline can collect data from it. A CivicClerk portal URL and a city's own website are equally correct if they serve the same city's council data.

The pipeline now uses **Serper.dev** as its primary search backend.

---

## Lessons Learned: Self-QA When Generating Briefings

The search engine comparison above was a relatively clean problem — one URL per city, easy to verify by inspection. The harder problem is QA-ing LLM-generated claims in briefings, where the source material is long, unstructured, and the failure modes are subtle.

### The Core Problem: Full-Context Prompting Doesn't Scale

The naive approach is to dump the entire agenda document into an LLM prompt and ask it to extract structured data. This fails in three ways:

1. **Hallucinations under long context.** When an LLM is given a 40-page agenda packet, it starts confabulating — inventing dollar amounts, attributing items to the wrong agenda body, or summarizing from memory rather than the actual text. The hallucinations are often plausible and hard to catch without going back to the source.

2. **Cross-contamination between agenda items.** A dollar figure from item 7 bleeds into the summary of item 3. A presenter from one meeting shows up attributed to a different one. The LLM conflates details across a long document and produces a coherent-sounding but incorrect briefing.

3. **Cost and latency don't scale.** Passing a full agenda packet on every generation pass — extraction, summarization, topic classification, QA — is expensive and slow. At 201 cities running weekly, the token cost compounds quickly.

### What Works: Narrow Context per Pass

The fix is to treat LLM passes like database queries: only pass in the data that is strictly necessary for what that pass is doing.

- **Extraction pass:** Send one agenda item at a time, not the full document. The LLM extracts structured fields (title, section, fiscal amounts, presenter) from a small, bounded chunk of text. This eliminates cross-item contamination.
- **Summarization pass:** Send only the structured extraction output — not the raw PDF text — as input. The LLM summarizes what was already extracted, not the raw source.
- **QA / verification pass:** For any claim the LLM generates (especially dollar amounts, vote outcomes, presenter names), pass the verbatim source sentence or paragraph alongside the claim and ask the model to verify. This is fast because the context is tiny, and it catches hallucinations at the point of generation rather than downstream.

### Verifiability as a Design Constraint

Every LLM-generated claim in a briefing should be traceable to a specific span of source text. Concretely:

- Dollar amounts must be verified against verbatim strings extracted from the PDF (e.g. `"$4.2 million"` must appear in the source text, not just be semantically implied).
- Agenda item titles should be copied verbatim, not paraphrased, to preserve searchability and avoid subtle meaning shifts.
- If a claim cannot be grounded in a specific source span, it should be flagged as unverified or omitted.

This constraint also makes QA tractable for humans. If a reviewer wants to check a briefing claim, they should be able to click through to the exact sentence in the source document — not re-read the entire 40-page packet to hunt it down.

### Practical Architecture

Each briefing generation run should produce three artifacts, not one:

1. **Structured extraction JSON** — one record per agenda item, fields populated from narrow per-item LLM passes
2. **Briefing output** — the human-readable summary, generated from the extraction JSON (not the raw source)
3. **Provenance log** — for each claim in the briefing, the source item index and the verbatim text span it was grounded in

QA then becomes a diff between the briefing output and the provenance log — mechanical enough to automate, small enough to review manually when something looks off.
