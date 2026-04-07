# City Viability Research: Agenda Data for Political Briefings
_April 2026 — For data researcher review_

## Summary from Pilot Testing (Human Perspective)

The following is a direct summary from the person who tested the pilot manually:

> "I found quite a few towns where I don't know if they post their agendas ahead of time. I have found a couple that seemingly haven't posted their agendas in months, so it is hard to know which category to group a city into until we have successfully obtained an upcoming agenda from them. There is also to note that it seems, at least for smaller towns, the calendars for when the next meetings are posted is not in the same place as the agendas. It's generally confusing even for a person trying to find them. They may not post agendas until right before the meeting, but until they are posted, it's hard to say where they would be if they were posted.
>
> In my tests, regardless the tool you use (automated, AI search/crawlers, or manual) the results are ambiguous.
>
> The bigger tools are easier to interpret and kind of work how you would expect. Small towns often use custom websites of varying quality ranging from decent to wtf.
>
> It does seem like finding when the next meeting is and whether or not they post agendas are two separate problems needing separate solutions. Also yes, you are supposed to post agendas ahead of time, but at least for small towns, that's often not always the case."

A colleague responded:
> "I wonder if we could raise the threshold for population size for the towns in the trial? Of the ones you named only 3 have more than 8000 people. For the trial/pilot this might not be worth solving. If you're wondering whether we can offer this as a product with nationwide availability at all then that might be another question. But perhaps there are enough towns with data available publicly/via APIs that it's worth proceeding."

**This document is an attempt to answer both questions: what is the right population/platform threshold for the pilot, and is this viable at scale?**

---

## The Core Question

Can we reliably collect city council agenda data that is **detailed enough to be useful** for political briefings? And if so, what distinguishes cities where this works from cities where it doesn't?

This document compiles everything we learned from a hands-on pilot across ~67 cities in NC, OH, and TX. It is intended to help a researcher identify criteria for viable candidates at scale.

---

## What We're Trying to Do

For each city, we want:
1. A URL that reliably points to where agendas are posted
2. The agenda document itself (PDF or structured data), posted before the meeting
3. Enough content in that document to extract meaningful agenda items

We then run the document through an LLM extraction pipeline to produce a structured briefing: agenda items, fiscal amounts, public hearings, vote categories.

---

## Issue 1: Many Small Cities Simply Don't Post Agendas Online

This was the most surprising finding. A significant fraction of small cities — especially townships and towns under ~10,000 population — do not publish agenda documents on the internet at all.

**Confirmed examples from our pilot:**
- **Lexington, OH** (~10K pop) — Website at `lexingtonohio.us/meetings` explicitly states: *"Agenda for Council meetings are available on Fridays before Council meetings upon request."* No digital posting.
- **Rootstown Township, OH** — Active website, meets every 2 weeks, posts minutes within days — but zero pre-meeting agendas. `rootstowntwp.com/trustees.php` has a full archive of minutes going back years, no agendas.
- **Chardon Township, OH** — No agenda documents found online. (Note: "City of Chardon OH" does post agendas on CivicClerk — but the Township of Chardon is a separate, smaller entity with the same name. Our candidate is from the township.)
- **Poland, OH** — Poland Township posts agendas at `polandtownship.gov`. Poland Village (where our candidate is) does not. Their events page at `polandvillage.org/home/events` shows only a Google Meet invite with no agenda document attached.
- **Walbridge, OH** — No agenda documents on their site
- **Elm City, NC** — Posts meeting minutes after the fact, but no pre-meeting agendas
- **Refugio, TX** — No city website; public notices only through county

**Hypothesis for researcher:** There may be a population or budget threshold below which digital agenda posting is not common. Ohio townships in particular seem to have low rates of digital agenda publishing. This could be testable at scale by sampling cities by population band and checking for agenda URL existence.

---

## Issue 2: Hard to Verify "This Is Where They Would Post It If They Did"

Even when we find a relevant page, it's often unclear whether:
- The page is actively maintained (vs. abandoned)
- Agendas are ever posted there pre-meeting (vs. only minutes after)
- The page we found is the right one (vs. a secondary board or archive)

**Specific examples:**
- **Mount Vernon, TX** — Found a Municode Meetings page with past agendas. Most recent is March 9. Could be monthly meetings (next would be April), or they may have switched platforms.
- **Clearcreek Township, OH** — Calendar page shows upcoming Trustee Meetings but no PDFs attached. A separate `/trustee-meetings/` page has PDFs, but most recent is March 23. Did not post the April 7 agenda before their meeting.

**The fundamental problem:** We need historical posting pattern data to distinguish "not posted yet" from "they don't post here anymore." A city that reliably posts 3 days before every meeting looks identical to a city that stopped posting 6 months ago — until we check again in 3 days.

**What would help:** A dataset of (city, platform_url, last_agenda_date, meeting_frequency) to identify cities with consistent posting histories.

---

## Issue 3: Agenda Content Is Often Too Thin for Small Cities

Even when we successfully collect an agenda, small-city agendas frequently lack the substance needed for a useful briefing.

**Content patterns we observed:**

| City Size / Type | Typical Agenda Content |
|-----------------|----------------------|
| Large city (Austin, Cleveland, Durham) | 20–40 items, detailed staff reports, fiscal analysis, public hearings, zoning cases |
| Mid-size city (Dublin OH ~50K, Marysville OH ~25K) | 15–26 items, contracts, ordinances, presentations, clear fiscal amounts |
| Small city on CivicClerk (Locust NC ~5K) | 6pp, annexation ordinance, speed limit reduction, $1.78M surplus, budget amendments — substantive |
| Township (Etna Township OH ~15K) | 3–6 items, routine approvals, minimal detail |
| Very small / rural (Poland Township OH ~2.5K) | 3–5 items, often just "approve minutes", "public comment", "new business" |

**Observed quality threshold:** Cities with fewer than ~10 substantive agenda items tend not to produce briefings worth reading. The LLM can extract the items, but there's not enough there — no fiscal amounts, no contested votes, no meaningful context for a constituent.

**Concrete example — Clearcreek Township, OH:**
The most recent agenda (March 23, 2026) lists items like "RESOLUTION 5661" with no description, no link to what the resolution covers, no fiscal amount. There is no way to infer what the resolution does from the agenda alone. Compare to Dublin, OH whose same-week agenda had 17 items including a $2.1M water meter replacement contract with full staff report.

Notably, Clearcreek Township also did not post their April 7 agenda until after the meeting date — despite the meeting being on the calendar. This is consistent with the "upon request" pattern: the agenda exists internally but isn't reliably published online in advance.

**The thin-agenda problem is structural, not a data collection issue.** Township trustee meetings in Ohio often cover road maintenance, zoning variances, and routine resolutions with no descriptions. Even perfect collection would yield a nearly empty briefing.

---

## Issue 4: City/Township Name Collisions Create Confusion

Several jurisdictions in our pilot share a name with a larger neighboring entity, and they have completely different digital presences and agenda practices:

- **Chardon Township vs. City of Chardon, OH** — City of Chardon has a full CivicClerk portal with Council meeting agendas. Chardon Township is a separate government covering the surrounding rural area — no portal, no agendas. Both are called "Chardon." Our candidate is from the township.
- **Poland Township vs. Poland Village, OH** — Poland Township posts agendas at `polandtownship.gov`. Poland Village is a separate incorporated village entirely — their site is under construction and meetings are posted as Google Meet invites with no agenda.
- **General pattern** — In Ohio especially, "City of X" and "X Township" are distinct governments that can coexist geographically. Search results intermix them. A candidate who says they're from "Poland" could mean either one.

**Implication for researcher:** Jurisdictional type (city vs. village vs. township vs. county) needs to be captured explicitly. Candidates from Ohio townships are likely to have worse data coverage than candidates from Ohio cities, even with the same city name.

## Issue 5: Meeting Schedule and Agenda Document Are Often on Separate Pages

Many cities maintain:
1. A **calendar page** — shows upcoming meeting dates and times, but no documents
2. An **agendas page** — shows PDFs, but only for past meetings or recent ones

These are often completely separate URLs with no link between them. Our pipeline tries to find both at once and can fail if it only finds the calendar (no PDFs) or the archive (no upcoming dates).

**Practical implication:** Verifying that a city "has agendas" requires checking both that (a) they have a platform where documents would be posted, and (b) they actually do post documents there — ideally by looking at the recency and consistency of past postings.

---

## Issue 6: Agenda Posting Timing Varies Widely — And Small Cities Post Late or Not At All

Cities post agendas anywhere from 1 day to 2 weeks before the meeting. This matters for our product:

- **Legistar cities** (Austin, Cleveland): agendas finalized and posted 5–7 days out, very consistent
- **CivicPlus cities**: typically 3–5 days before
- **Small cities on custom sites**: anywhere from 1–7 days, inconsistent
- **Very small cities**: sometimes same-day or not at all until after

For a briefing product, the agenda needs to be posted with enough lead time for the official to review it. Cities that post 24 hours before are borderline useful.

---

## Issue 7: Platform Matters Enormously for Reliability

Cities on structured civic tech platforms are far more reliable than cities on generic CMS/WordPress sites:

| Platform | Reliability | API Available | Coverage |
|----------|------------|--------------|---------|
| Legistar | Excellent | Yes (REST) | Large cities |
| CivicClerk | Excellent | Yes (OData) | Mid-size cities |
| Granicus/Swagit | Good | RSS/HTML | Mid-size cities |
| CivicPlus AgendaCenter | Good | AJAX scrape | Small-mid cities |
| Municode Meetings | Good | HTML | Small-mid cities |
| eSCRIBE | Good | Portal scrape | Mid cities |
| Custom WordPress/CMS | Poor | None | Small cities |
| Township custom sites | Very poor | None | Rural townships |
| No platform | None | — | Rural/tiny cities |

---

## What Viability Looks Like: Proposed Criteria for Researcher to Test

Based on our experience, a "viable" city for this product likely has **all** of the following:

1. **Population ≥ 15,000–20,000** — below this, agendas are often not posted or too thin
2. **On a structured civic platform** (Legistar, CivicClerk, Granicus, CivicPlus, Municode) — not a custom site or WordPress
3. **Consistent posting history** — agendas posted for ≥ 10 of the last 12 months' meetings
4. **Agenda length** — ≥ 10 substantive items (excludes call to order, approve minutes, adjournment)
5. **Posts ≥ 3 days before meeting** — enough lead time for the official to review the briefing
6. **Not a school board or special district** — only primary governing body (City Council, Town Council, Village Council, Township Trustees)

**Probably not viable:**
- Ohio townships under ~15K population
- Texas cities under ~10K without a civic platform
- Any city where we can find a meeting calendar but no agenda documents
- Any city where the most recent agenda is > 60 days old

---

## April 7–9 Meeting Deep Dive: Is the Agenda Content Actually Useful?

We collected PDFs for 6 of the 8 cities with April 7–9 meetings. Here is the content quality assessment for each:

| City | Pop | Platform | Agenda Posted? | Pages | Words | Dollar Amounts | Assessment |
|------|-----|----------|---------------|-------|-------|---------------|------------|
| **Kyle, TX** | ~50K | Granicus | ✅ Apr 7 | 8pp | 2,814 | $120K–$2.9M | ✅ Rich — consent agenda, contracts, water rate legal matter, proclamations |
| **Johnstown, OH** | ~5K | CivicClerk | ✅ Apr 7 | 152pp | 53,510 | $45K–$63M | ✅ Rich packet — full staff reports, contracts, legislation |
| **Brecksville, OH** | ~14K | CivicClerk | ✅ Apr 7 | 51pp | 15,511 | $2.5K–$8.7K | ✅ Solid — 51 items, ordinances, contracts, tax applications |
| **Etna Township, OH** | ~15K | agent | ✅ Apr 7 | 2pp | 516 | none | ❌ Too thin — "Call to order, Roll Call, Fiscal Officer Report" — no substance |
| **Clearcreek Township, OH** | ~13K | agent | ❌ Not posted | 2pp | 645 | $10K | ❌ Prior month only; items are "RESOLUTION 5639" with no description |
| **Sandy Oaks, TX** | ~5K | Custom WordPress | ✅ Apr 9 | 3pp | 1,381 | none | ⚠️ Borderline — town hall on proposed tax increase, CDBG grant, ordinance changes, but no dollar amounts |
| **Poland, OH (Village)** | ~2.5K | — | ❌ No agenda | — | — | — | ❌ Google Meet invite only |
| **Locust, NC** | ~5K | CivicClerk | ✅ Apr 9 | 6pp | 1,716 | $1.79M–$2.1M | ✅ Solid — annexation ordinance, speed limit reduction, budget amendment, $1.78M surplus figure |

### Key Findings

**Johnstown OH is the surprise.** At only ~5K population it's on CivicClerk with a 152-page agenda packet including $63M in financial items and full staff reports. This breaks the population rule — **platform matters more than population size**. A 5K city on CivicClerk can have richer data than a 50K city on Granicus.

**Kyle TX is actually ✅ rich data** — the initial assessment was wrong because the collection script was reading a cached January work session PDF instead of fetching the April 7 document from the Granicus AgendaViewer URL. The actual April 7 agenda is a full regular City Council meeting: consent agenda with $120K trail master plan, water rate legal dispute (PUC appeal), opioid settlement, and $2.9M in total action items across 8 pages. This is exactly the kind of briefing-ready content we want. Note: Granicus `AgendaViewer.php` URLs redirect to the real PDF — the collector needs to follow that redirect rather than rely on locally cached PDFs from prior runs.

**The townships (Etna, Clearcreek) confirm the earlier pattern.** Even when we get the PDF, it's just a list of procedural items and opaque resolution numbers. No context, no descriptions, no fiscal amounts. Not useful for a briefing.

**The platform signal is strong:**
- All 3 structured-platform cities (CivicClerk × 2, Granicus × 1) have substantive agendas — even at 5K population (Johnstown)
- Both township/custom-site cities have thin or missing agendas regardless of population
- Platform type is a better predictor of content quality than city size

## What We Know: April 7–9 Meetings from the HubSpot List

These are the 8 cities from the HubSpot list with meetings April 7–9. This is what we can state with confidence.

| City | Pop | Platform | Agenda | Content Quality |
|------|-----|----------|--------|----------------|
| **Kyle, TX** | ~50K | Granicus | ✅ Collected | ✅ Rich — 8pp, $2.9M in action items, consent agenda, legal matter |
| **Johnstown, OH** | ~5K | CivicClerk | ✅ Collected | ✅ Rich — 152pp packet, $63M bond, contracts, full staff reports |
| **Brecksville, OH** | ~14K | CivicClerk | ✅ Collected | ✅ Solid — 51pp, ordinances, contracts, tax applications |
| **Etna Township, OH** | ~15K | Custom site | ✅ Collected | ❌ Thin — 2pp, 516 words, "Call to order / Fiscal Officer Report", no substance |
| **Clearcreek Township, OH** | ~13K | Custom site | ❌ Not posted | ❌ Prior month only; items are opaque ("RESOLUTION 5661") with no descriptions |
| **Poland, OH (Village)** | ~2.5K | None | ❌ No agenda | ❌ Google Meet invite only, no document |
| **Locust, NC** | ~5K | CivicClerk | ✅ Collected | ✅ Solid — 6pp, 1,716 words, $1.78M surplus, annexation ordinance, speed limit reduction |
| **Sandy Oaks, TX** | ~5K | Custom WordPress | ✅ Collected | ⚠️ Borderline — 3pp, 11 items, town hall on proposed tax increase, CDBG grant, personnel policy, no dollar figures |

**What this tells us:** All 4 cities on structured platforms (Granicus, CivicClerk) have actionable agendas — even at 5K population (Johnstown, Locust). The 3 cities on custom sites or no platform all have either missing or thin agendas. Platform type is a stronger predictor of content quality than city size.

**What we can't say with confidence about the rest of the 45-city list:** Many cities we have not yet verified with an actual upcoming agenda. We may have found URLs that are plausible but unconfirmed — a site that *would* post agendas if they were ready, but we haven't seen one posted there yet. Until we collect an actual upcoming agenda from a city, its status is uncertain regardless of what discovery found.

---

## Addressing the Population Threshold Question Directly

The colleague's observation that "only 3 of the named cities have more than 8,000 people" is the crux of the issue. Our April 7–9 data gives us a clearer answer than we had before.

**Population alone is not the right filter. Platform type is.**

From the April 7–9 deep dive:

| City | Pop | Platform | Content |
|------|-----|----------|---------|
| Johnstown, OH | ~5K | CivicClerk | ✅ 152pp packet, $63M in items, full staff reports |
| Locust, NC | ~5K | CivicClerk | ✅ 6pp, annexation ordinance, $1.78M surplus |
| Brecksville, OH | ~14K | CivicClerk | ✅ 51pp, ordinances, contracts, tax applications |
| Kyle, TX | ~50K | Granicus | ✅ 8pp, $2.9M in action items |
| Etna Township, OH | ~15K | Custom site | ❌ 2pp, "Call to order / Fiscal Officer Report", no substance |
| Clearcreek Township, OH | ~13K | Custom site | ❌ Opaque resolution numbers, no descriptions |
| Poland Village, OH | ~2.5K | None | ❌ Google Meet invite only |

**The pattern:** Johnstown and Locust are both ~5K population and both produce substantive, useful agendas — because they're on CivicClerk. Meanwhile Etna Township at 15K is useless. Population doesn't explain the difference; the civic platform does.

**What the data suggests the real filter should be:**

| Filter | Effect |
|--------|--------|
| Population ≥ 15K | Eliminates some bad cities but also cuts Johnstown and Locust, which work well |
| Structured platform (Legistar, CivicClerk, CivicPlus, Granicus) | Correctly separates useful from not-useful across all sizes in our sample |
| Exclude townships (OH, PA, NJ, etc.) | Removes a class of consistently thin agendas regardless of population |

**The pilot/trial question and the scale question are different:**

| Question | Answer |
|----------|--------|
| Should we filter the pilot to structured platforms only? | Yes. Custom sites and no-platform cities consistently fail — on content quality, on availability, or both. |
| Does population matter at all? | Somewhat — very small cities (under ~5K) are higher risk even on good platforms, and are less likely to have a structured platform in the first place. But 5K on CivicClerk beats 15K on a custom site. |
| Is this viable nationwide at larger city sizes? | Very likely yes. There are ~3,000+ US cities with 15K–500K population, the majority of which use Legistar, CivicClerk, CivicPlus, or Granicus. These cities have rich, consistent agenda data. |
| How many US elected officials are in that addressable segment? | Unknown — this is the key research question. City council members in structured-platform cities number in the tens of thousands. The data exists; we just need to quantify the overlap with our candidate pipeline. |

**Recommendation for the pilot:** Filter to cities on a structured civic platform (Legistar, CivicClerk, CivicPlus, Granicus) AND exclude townships. Population is a secondary signal — prefer ≥10K but don't discard a 5K city just for being small if it's on a good platform. The custom-site problem is real but solvable later with manual curation — it's not the right thing to solve first.

---

## Recommended Research Questions

For the data researcher, the most valuable investigation would be:

1. **Is population a reliable predictor of agenda availability?** Sample 200 cities across population bands (5K, 10K, 20K, 50K, 100K+) and check whether they have a structured civic platform.

2. **Is platform adoption a reliable predictor of content quality?** For cities on Legistar vs. CivicPlus vs. custom CMS, what is the average agenda item count?

3. **What fraction of elected officials in our target demo (running for or recently won office in cities 10K–100K) are in cities with viable agenda data?** This tells us the addressable market size before we invest in scaling the pipeline.

4. **Are there state-level patterns?** Ohio townships seem systematically worse than Ohio cities. Texas cities seem to have higher platform adoption than NC cities of similar size. Is this generalizable?

5. **Can we score cities on a "viability index" from public data alone?** Inputs: population (Census), platform type (ICMA survey or web scrape), most recent agenda URL age (our crawler), agenda PDF word count.
