# Collection Agent Failure Analysis
_April 2026_

## Why a Human Can Find Agendas But Our Pipeline Can't

This document captures a systematic analysis of why the automated collection pipeline fails on cities where a human can Google the answer in 30 seconds. Understanding this is critical to improving coverage from ~32/44 to ~44/44 pilot candidates.

---

## Root Causes

### 1. Tavily ≠ Google
Tavily is a specialized search API with a much smaller index than Google, especially for small local government sites. When you Google "Hartville Ohio council agendas" the first result is their council page. Tavily returns nothing. The pipeline then has no URL to start from, so the entire downstream pipeline (Playwright, deep probes, agent) never fires.

**Fix applied:** Added Claude WebSearch as a fallback in `source_discover.py` when Tavily returns 0 candidates. Claude's web search covers far more of the long-tail web. Cost: ~$0.01/city.

---

### 2. Discovery Finds the Wrong URL (Secondary Platform Instead of Primary Site)
This is equally bad as finding nothing — the pipeline has a URL, proceeds with confidence, and silently fails downstream. Tavily sometimes surfaces a secondary or embedded platform portal instead of the city's own website.

**Concrete example — Etna Township OH:**
- Tavily found: `etnatownship.diligent.community` (a Diligent board portal, likely requires login)
- The real site with public agendas: `https://etnatownship.com/calendar/` — the **first Google result** for "Etna Township Ohio council meeting agendas"
- The agent couldn't navigate the Diligent portal and failed. After updating source.json to the correct URL, the agent found the April 7, 2026 Board of Trustees agenda in 2 steps.

**Why this happens:** Civic tech platforms (Diligent, CivicClerk, Legistar) are heavily indexed and often appear before the city's own website in specialized search engines like Tavily, even when the city's own site has the better public access. Google tends to surface the primary city site first because it weighs domain authority and direct navigation signals.

**The insight:** The problem isn't only when Tavily returns zero results — it's equally bad when Tavily returns the *wrong* URL. We should use Claude WebSearch as a validation step whenever no *fresh* source is found at the initially discovered URL, not only when candidate count is zero.

**Fix applied:** Added `discover_official_domain_via_claude()` as fallback in discovery retry 2. Known-sources registry updated with correct URLs for Etna Township and others.

---

### 3. The Agent Has Artificial Body Name Constraints
We told the agent to only collect "City Council" meetings. So when it lands on Etna Township's page and sees "Board of Trustees" meetings, it explicitly skips them — even though the Board of Trustees *is* the governing body for Ohio townships.

A human just sees "trustees" and knows that's what they want.

**Fix applied:** Expanded the prompt to explicitly include "Board of Trustees" (for townships), "Board of Aldermen", and other governing body variants. Body name constraint moved from hard filter to soft preference. Also added "trustees" and "board of trustees" to `AGENDA_KEYWORDS` in `document_verifier.py`.

---

### 4. The Verifier Is Too Strict and Rejects Valid Content
The original `document_verifier.py` expected either:
- A PDF with `%PDF-` header and agenda keywords in first-page text
- An HTML page with `<html` or `<!doctype` and agenda keywords

This means it rejected:
- **Google Drive folder URLs** — the Drive folder page is valid HTML but keywords like "agenda" aren't in the folder's HTML, they're in the filenames. Hartville's agendas live in a Drive folder. Agent found them correctly; verifier rejected them.
- **Legistar/CivicClerk agenda viewer pages** — some return HTML that doesn't contain agenda keywords in the first 8KB fetch.
- **Google Docs / embedded viewers** — some cities link to docs.google.com or embedded PDF viewers.
- **Redirect URLs** — some CivicPlus/CivicClerk URLs redirect before serving content; partial fetch misses the actual document.

A human just opens the Drive folder and sees the PDFs.

**Fix applied:**
- Added `_DOCUMENT_HOSTING_DOMAINS` whitelist: Google Drive, Google Docs, Dropbox, OneDrive, SharePoint, Box — accepted on date-range check alone (content not directly fetchable without auth).
- Added `_KNOWN_PLATFORM_PATTERNS` whitelist: Legistar, CivicClerk, CivicPlus, eSCRIBE, Diligent, BoardDocs, Granicus, Swagit, Novus Agenda, Municode, PrimeGov, DestinyHosted, Haystaq, CivicWeb — accepted if server returns 200 and date is valid; keyword check skipped.
- Added `_is_valid_date()` helper; `verify_events` now rejects events with missing/unknown/unparseable dates before even fetching URLs.

---

### 5. Domain Path Probing Uses Fixed Guesses
When discovery finds a city's domain, it tries common paths like `/AgendaCenter`, `/agendas`, `/city-council`, `/government/agendas-minutes`.

Hartville's actual council page is at `/wba/content/meetings-and-news/council/` — a completely custom CMS path. The fixed path list will never find it.

A human just clicks navigation links until they find it.

**Fix:** After fixed path probing fails, hand the domain to the browser agent to navigate and find the agendas section. This is exactly what the agent is good at.

---

### 6. The Agent Gives Up Too Early or Hallucinates
When the agent can't find something after a few steps, it sometimes:
- Returns the main site URL as the agenda URL
- Returns prose explanation instead of JSON
- Navigates to YouTube or Facebook
- Returns archive/index pages with `date: unknown`

**Fix applied:**
- Added JSON regex fallback: `re.search(r"\[.*\]", raw, re.DOTALL)` extracts embedded JSON from prose responses.
- Prompt rule: "NEVER leave the city's official website."
- Prompt rule: "If stuck on same page 2-3 steps without progress, try a DIFFERENT navigation path — go back to homepage, look for a different menu item, try searching the site."
- Prompt rule: Step 2 now says "IMMEDIATELY scroll down and expand ALL collapsed sections before searching" — catches accordion-hidden content like Walton Hills OH's year-collapsed agenda list.

---

## Summary of Fixes

| Problem | Fix | Status |
|---------|-----|--------|
| Tavily misses small cities | Claude WebSearch fallback in `source_discover.py` | ✅ Done |
| Discovery finds wrong URL (secondary platform) | Known-sources registry; Claude WebSearch as validation | ✅ Done |
| Agent skips township trustees | Expanded body name list in `reason.py` prompt + verifier keywords | ✅ Done |
| Verifier rejects Google Drive | `_DOCUMENT_HOSTING_DOMAINS` whitelist in `document_verifier.py` | ✅ Done |
| Verifier rejects known platform viewers | `_KNOWN_PLATFORM_PATTERNS` whitelist in `document_verifier.py` | ✅ Done |
| Fixed path probing misses custom CMS | Pass unresolved domains to browser agent | 🔧 Pending |
| Agent gives up too early / goes off-site | Reframed "give up" and "stay on site" instructions in prompt | ✅ Done |
| Agent returns prose instead of JSON | Regex fallback JSON extractor in `reason.py` | ✅ Done |
| Agent misses accordion-hidden content | Explicit "expand all collapsed sections" instruction in prompt | ✅ Done |

---

## Cities Still Blocked After All Fixes

### Architectural note: Meeting schedule vs. agenda documents are often on separate pages

Many cities maintain two separate things:
1. **Meeting calendar** — recurring dates, times, location. Often on a `/calendar/` or events page. Always available, even weeks out.
2. **Agenda documents** — the actual agenda PDF, posted 3–7 days before the meeting. On a separate `/agendas/` or `/trustees/` or platform page.

Our current pipeline tries to find both at once. This creates false negatives: we find the calendar, see no agenda PDF, and conclude "nothing available." In reality the agenda just isn't posted yet.

**Implication:** For cities where we've confirmed the agenda platform exists but no current document is posted, we should run a **second pass** closer to the meeting date (e.g. 3 days before the scheduled meeting) rather than treating it as a permanent failure.

Examples:
- **Lago Vista TX** — Granicus viewer is the right place; agenda not posted yet for next meeting
- **Mount Vernon TX** — Municode Meetings is the right place; agenda not posted yet for next meeting
- **Clearcreek Township OH** — `/trustee-meetings/` page has agendas but they're posted ~1 week before

### No upcoming agenda posted yet (platform found, past meetings collected)

- **Lago Vista TX** — Granicus viewer at `lagovistatexas.granicus.com/ViewPublisher.php?view_id=1`. 5 past City Council meetings collected (most recent Feb 20). No upcoming agenda posted yet.
- **Mount Vernon TX** — Municode Meetings at `mountvernon-tx.municodemeetings.com`. 4 past City Council meetings collected (most recent Mar 9). No upcoming agenda posted yet.

### Don't publish agendas online (confirmed by manual Google search)

- **Lexington OH** — Explicitly states: "Agendas available on Fridays before meetings upon request." No digital posting.
- **Rootstown Township OH** — No agenda documents found online
- **Chardon Township OH** (township, not city) — No agenda documents found online

### Other genuine blockers

- **Pflugerville TX** — Legistar instance not configured for public agenda access (admin setting)
- **Sandy Oaks TX** — Website unreachable/down
- **Palestine TX** — Meeting exists, no agenda document posted yet
- **Canal Fulton OH** — Returns 403 to all automated access including browser UA
- **Pembroke NC** — Stale website, no agendas posted
- **Refugio TX** — No city website; only county public notices
- **Elm City NC** — Posts minutes only, not agendas
- **Walbridge OH** — No agenda documents on their site

### Resolved
- **Clearcreek Township OH** — ✅ Agendas at `/trustee-meetings/`, not the calendar page
- **Etna Township OH** — ✅ Correct URL: `etnatownship.com/calendar/` (Tavily found Diligent portal instead)
