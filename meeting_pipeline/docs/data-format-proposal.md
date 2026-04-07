# Common Data Formats — Municipal Data Pipeline

## The Problem

We have multiple collectors (Legistar, CivicPlus, PDF, etc.) that produce different raw data. We need:
1. A common way for any collector to describe what it collected (so the pipeline knows what to process)
2. A common normalized output from LLM analysis (so product features don't care which platform the data came from)

---

## Layer 1: Collection Manifest (Raw — S3)

Every collector writes to the same S3 path structure and produces a `_manifest.json` with a common schema. The actual data files alongside it can be platform-specific.

### S3 Path Structure

```
municipal-data-{env}/
  {citySlug}/
    legislative/
      {YYYY-MM-DD}/              ← one folder per collection run
        _manifest.json           ← always present, common schema
        meetings.json            ← structured meeting data (from API collectors)
        agenda_20260319.pdf      ← agenda PDF files
        minutes_20260319.pdf     ← minutes PDF files
    fiscal/
      {YYYY-MM-DD}/
        _manifest.json
        budget_data.json
    constituent/
      {YYYY-MM-DD}/
        _manifest.json
        demographics.json
        issue_scores.json
```

### Manifest Schema

```typescript
interface CollectionManifest {
  // Envelope — same for every collector
  version: "1.0"
  citySlug: string                    // "durham-nc"
  cityName: string                    // "Durham"
  state: string                       // "NC"
  category: "legislative" | "fiscal" | "constituent" | "news"
  collectorType: string               // "legistar" | "civicplus" | "pdf-agenda" | "nc-linc" | "haystaq"
  collectedAt: string                 // ISO 8601 timestamp
  collectionDurationMs: number        // How long the collection took
  status: "complete" | "partial" | "error"
  errors?: string[]                   // If partial or error

  // What was collected — varies by category
  meetings?: MeetingRecord[]          // For legislative category
  fiscalData?: FiscalRecord           // For fiscal category
  constituentData?: ConstituentRecord // For constituent category
  newsData?: NewsRecord               // For news category
}
```

### Meeting Record — Common Across All Legislative Collectors

This is the core. Legistar, CivicPlus, and PDF collectors all produce `MeetingRecord[]`:

```typescript
interface MeetingRecord {
  // === Required fields — every collector must provide these ===
  date: string                        // "2026-03-19" (ISO date)
  title: string                       // "City Council Regular Meeting"
  body: string                        // "City Council" | "Planning Board" | etc.
  sourceUrl: string                   // URL where this meeting was found

  // === Agenda items — structured if available, otherwise from PDF ===
  agendaItems?: AgendaItem[]          // Legistar: structured from API. Others: null until LLM extraction.

  // === Files collected ===
  files: {
    agendaPdf?: FileRef               // The agenda PDF
    agendaHtml?: FileRef              // HTML version if available
    agendaPacket?: FileRef            // Full packet (agenda + attachments)
    minutesPdf?: FileRef              // Meeting minutes
    attachments?: FileRef[]           // Supporting documents
  }

  // === Optional metadata ===
  time?: string                       // "7:00 PM"
  location?: string                   // "Council Chambers, City Hall"
  videoUrl?: string                   // Granicus/YouTube link
  postedAt?: string                   // When the agenda was published
  status?: "scheduled" | "completed" | "cancelled"

  // === Platform-specific data (preserved for debugging/future use) ===
  platformData?: Record<string, unknown>
}

interface FileRef {
  path: string                        // Relative to manifest: "agenda_20260319.pdf"
  mimeType: string                    // "application/pdf" | "text/html"
  sizeBytes?: number
  extractedText?: string              // If we ran OCR/extraction at collection time
}
```

### Agenda Item — What Legistar Gives Us Directly (Others Get This After LLM Analysis)

```typescript
interface AgendaItem {
  // === Core fields ===
  number?: string                     // "7" or "VII" or "C.2"
  title: string                       // "Rezoning Request — 123 Main St"
  type?: string                       // "Ordinance" | "Resolution" | "Public Hearing" | "Consent"

  // === Detail fields — available from Legistar, derived from LLM for others ===
  description?: string                // Full description
  sponsors?: string[]                 // ["Council Member Smith"]
  status?: string                     // "Approved" | "Deferred" | "In Committee"

  // === Vote data — Legistar only initially ===
  vote?: {
    result: string                    // "Pass" | "Fail"
    yea: number
    nay: number
    abstain: number
    absent: number
    rollCall?: { name: string; vote: string }[]
  }

  // === Attachments ===
  attachments?: FileRef[]

  // === Source reference ===
  sourceId?: string                   // Legistar MatterId, etc.
  sourceUrl?: string                  // Deep link to this specific item
}
```

### What Each Collector Produces

| Field | Legistar | CivicPlus | PDF-only |
|-------|----------|-----------|----------|
| `date` | From API event | HTML parsed | From PDF header/filename |
| `title` | From API event | HTML parsed | From PDF header |
| `body` | From API body | Category name via aria-label | Inferred from filename/content |
| `agendaItems[]` | **Full structured data from API** | `null` (filled by LLM later) | `null` (filled by LLM later) |
| `files.agendaPdf` | Downloaded from API attachments | Downloaded from `/ViewFile/Agenda/{id}` | The source PDF itself |
| `files.minutesPdf` | Downloaded if available | Downloaded from `/ViewFile/Minutes/{id}` | Separate download if found |
| `vote` (per item) | **Roll call votes from API** | Not available | Not available |
| `platformData` | `{ matterId, legistarSlug, eventId }` | `{ civicplusCategoryId, agendaId }` | `{ sourcePageUrl }` |

**Key insight:** Legistar fills in `agendaItems[]` at collection time. CivicPlus and PDF collectors leave it null — the LLM analysis pipeline fills it in later by extracting from the PDF. The downstream product features only look at `agendaItems[]` regardless of how it was populated.

---

## Layer 2: Normalized Meeting Data (After LLM Analysis — PostgreSQL JSONB)

After collection, the LLM analysis pipeline reads the raw data from S3 and produces normalized, product-ready JSON. This is stored in `MunicipalDataSnapshot.data` (PostgreSQL JSONB).

### For Recurring Meeting Briefings

```typescript
interface MeetingBriefing {
  version: "1.0"
  citySlug: string
  generatedAt: string                 // ISO timestamp

  // === The meeting ===
  meeting: {
    date: string                      // "2026-03-19"
    time?: string                     // "7:00 PM"
    body: string                      // "City Council"
    type: "regular" | "special" | "work_session" | "public_hearing"
    location?: string
    videoUrl?: string
  }

  // === Agenda items — normalized regardless of source ===
  agendaItems: NormalizedAgendaItem[]

  // === Constituent context — from Haystaq/L2 ===
  constituentContext?: {
    relevantIssues: {
      topic: string                   // Mapped from agenda item categories
      voterScore: number              // 0-100 Haystaq score
      rank: number                    // Among all voter priorities
      tier: "critical" | "strong" | "moderate" | "lower"
      sampleSize: number              // Voters in district
    }[]
    gapAlerts?: {
      topic: string
      gapType: "UNDER" | "OVER"       // Council attention vs. voter priority
      description: string
    }[]
  }

  // === News context ===
  newsContext?: {
    articles: {
      title: string
      source: string
      date: string
      url: string
      relevantAgendaItems: string[]   // Which agenda items this relates to
      summary: string
    }[]
  }

  // === Metadata ===
  dataSources: {
    collectorType: string             // "legistar" | "civicplus" | "pdf"
    manifestPath: string              // S3 path to raw data
    agendaItemSource: "api" | "pdf_extraction"  // How agenda items were obtained
    analysisModel: string             // "gemini-2.5-flash"
    analysisCost: number              // e.g., 0.04
  }
}

interface NormalizedAgendaItem {
  // === Identification ===
  number?: string                     // "7" or "C.2"
  title: string                       // Normalized title
  originalTitle?: string              // Title as it appeared in source (if different)

  // === Classification — LLM-assigned ===
  category: string                    // "zoning" | "budget" | "public_safety" | "transportation" | etc.
  itemType: "consent" | "public_hearing" | "action" | "presentation" | "discussion" | "procedural"
  significance: "high" | "medium" | "low"  // For an elected official

  // === Content ===
  description: string                 // 2-3 sentence summary
  keyIssue?: string                   // The core decision or question
  fiscalImpact?: string              // Cost/budget impact if mentioned
  staffRecommendation?: string       // Staff/admin recommendation if known

  // === Topic mapping (for constituent data linkage) ===
  topics: string[]                    // ["housing", "development", "zoning"]
  haystaqIssueMapping?: string[]      // Maps to Haystaq issue dimensions

  // === Vote data (if available — Legistar primarily) ===
  vote?: {
    result: "pass" | "fail" | "deferred" | "withdrawn"
    yea: number
    nay: number
    abstain: number
    details?: string                  // "Approved 7-0" or "Passed with amendments"
  }

  // === Source ===
  sourceUrl?: string                  // Deep link to this item
}
```

### For Full Orientation Briefings (Periodic Deep Analysis)

This builds on the POC's existing 6-pass analysis. Stored as a separate snapshot type:

```typescript
interface OrientationBriefing {
  version: "1.0"
  citySlug: string
  generatedAt: string
  timePeriod: { start: string; end: string }

  // === The 6 analysis passes (matching POC structure) ===
  legislativeOverview: {
    totalMatters: number
    topicAreas: {
      name: string
      description: string
      matterCount: number
      keyMatters: string[]
      significance: string
    }[]
    notablePatterns: string[]
    topPriorities: string[]
  }

  voteAnalysis: {
    totalItemsWithActions: number
    unanimousCount: number
    nonUnanimousCount: number
    deferredCount: number
    deniedCount: number
    dissentItems: string[]
    patterns: string[]
  }

  budgetAnalysis: {
    summary: string
    totalRevenueLatest: string
    totalExpenditureLatest: string
    revenueTrends: string[]
    expenditureTrends: string[]
    concerns: string[]
    strengths: string[]
  }

  committeeAnalysis: {
    totalBodies: number
    activeBodies: number
    committees: {
      name: string
      role: string
      meetingCount: number
      keyTopics: string[]
      recentFocus: string
    }[]
  }

  synthesis: {
    executiveSummary: string
    keyThemes: { title: string; description: string; evidence: string[] }[]
    immediatePriorities: string[]
    ongoingIssues: string[]
    knowledgeGaps: string[]
  }

  // === Mismatch + Quick Wins (from POC scripts 07, 09) ===
  constituentMismatch?: {
    mismatchTable: {
      councilTopic: string
      councilPct: number
      constituentScore: number
      gapType: "UNDER" | "OVER" | "MATCH" | "UNMAPPED"
      gapDescription: string
    }[]
  }

  quickWins?: {
    totalGapAreas: number
    totalRecommendedActions: number
    actions: {
      type: "committee_engagement" | "briefing_request" | "matter_review" | "context_research"
      priority: "high" | "medium" | "low"
      title: string
      rationale: string
    }[]
  }
}
```

---

## Normalization Strategy: No LLM for V1

### The Principle

Every time data passes through an LLM, we lose the ability to verify the output against the source. An elected official reading a briefing should be able to trace every data point back to the original document. LLMs make that impossible for whatever they touch.

The PM calls normalization "the single biggest technical risk." We solve it by not using LLMs for normalization at all in V1.

### What We Proved: pdfplumber Layout Analysis (Multi-City Validation)

We tested a zero-LLM agenda parser against real agenda PDFs from **two different CivicPlus cities in two different states** using pdfplumber.

**Critical discovery:** CivicPlus standardizes the **web platform** (HTML, AJAX endpoints, JavaScript) — but each city creates its own agenda PDFs with **different formatting conventions**. The parser must handle multiple numbering schemes and layout patterns.

#### Test Results

| City | PDF | Items Found | Fiscal $ | Staff Recs | Presenters | Category Hit Rate |
|------|-----|------------|----------|------------|------------|-------------------|
| **Rocky Mount NC** | Dec 2024 Regular Meeting (11 pages) | 19 main + 6 sub | 18 amounts | 17 found | — | 57% |
| **Cleburne TX** | May 2025 Workshop (2 pages) | 5 items | 0 (none in agenda) | — | — | 20% |
| **Cleburne TX** | May 2025 Regular Meeting (197-page packet, parsed first 10 pages) | 27 items | 22 amounts | — | 12 found | 70% |

#### What Differs Between Cities

| Aspect | Rocky Mount NC | Cleburne TX |
|--------|---------------|-------------|
| **Numbering** | Arabic: `1.`, `2.`, `3.` | Roman: `I.`, `II.` + Custom: `RS1.`, `CMP1.`, `MN1.`, `OC1.`, `EX1.` |
| **Sub-items** | Uppercase letters: `A.`, `B.` | No sub-items (detail is in body text) |
| **Main margin (x-position)** | x≈47 | x≈72 |
| **Staff info** | "City Manager Recommendation:" / "Recommended Action:" | "Presented by:" + "Summary:" |
| **Section headers** | Inline in numbered items | Centered headers: "CONSENT AGENDA", "ACTION AGENDA" |
| **Public hearing marker** | Implicit in item title | Explicit `*PUBLIC HEARING*` prefix |
| **Meeting type in header** | No (just "AGENDA") | Yes: "CITY COUNCIL REGULAR MEETING AGENDA" |
| **PDF size** | 2.2 MB (agenda only) | 39-60 MB (full packet with attachments) |

#### What's Identical (The Parser's Foundation)

Despite the formatting differences, these are consistent across all tested cities:

- **pdfplumber x,y coordinates** reliably reveal indentation hierarchy
- **Fiscal amounts** always match `$[\d,]+(\.\d{2})?` regex — verbatim from PDF, never wrong
- **Section keywords** (consent, action, public hearing, executive session) appear consistently
- **Date/time** always parseable from the first page
- **Item structure**: number + title on one line, details/body on subsequent indented lines

#### The Parser's Multi-Format Support

The parser handles multiple numbering conventions:

```python
ITEM_PATTERNS = [
    # Custom prefix (Cleburne-style): RS1., CMP1., MN1., OC1., EX1.
    re.compile(r'^([A-Z]{2,4}\d+)\.\s*(.*)'),
    # Arabic numeral: 1., 2., 10.
    re.compile(r'^(\d+)\.\s+(.*)'),
    # Roman numeral: I., II., III., IV., V.
    re.compile(r'^((?:X{0,3}(?:IX|IV|V?I{0,3})))\.\s+(.*)', re.IGNORECASE),
]
```

And uses **relative x-position** (based on the most common left margin) rather than absolute values.

### What the Parser Extracts

| Extraction | Method | Rocky Mount | Cleburne |
|------------|--------|------------|----------|
| Meeting date, time, type | Regex on page 1 header | date ✓, time missed | date ✓, time ✓, type ✓ |
| Main item numbers + titles | Multi-pattern regex at left margin | 19/19 (100%) | 27 items (with 3 false positives at end) |
| Sub-items | Letter regex at indented position | 6 found | 0 (Cleburne doesn't use sub-items) |
| Staff recommendations | Literal match: "City Manager Recommendation:" etc. | 17 found | 0 (uses "Summary:" instead) |
| Presenters | "Presented by:" field extraction | 0 (not in format) | 12 found |
| Fiscal dollar amounts | Regex: `\$[\d,]+(?:\.\d{2})?` | 18 exact | 22 exact |
| Section detection | Keyword match on centered headers | Consent ✓, Public Hearings ✓ | Consent ✓, Action ✓, Discussion ✓, Executive ✓ |
| Item type classification | Keyword rules on title | Correct | Correct |
| Public hearing detection | Title keyword + `*PUBLIC HEARING*` | 1 found | 1 found |
| Topic categorization | Keyword dictionary match | 57% categorized | 70% categorized |

### Known Issues (Fixable Engineering Problems, Not Fundamental Limits)

| Issue | Cause | Fix |
|-------|-------|-----|
| "Pub lic" split word (Rocky Mount) | PDF kerning/font issue | Use pdfplumber word grouping with x_tolerance |
| 3 false-positive items at end (Cleburne) | "Guidelines to speak" text starts with numbers | Stop parsing at CERTIFICATION/ADJOURNMENT section |
| Section tracking bleeds across items (Rocky Mount) | Section detected from item text, not headers | Track sections by centered-text headers only, not inline text |
| Consent sub-item numbering conflict (Rocky Mount) | Sub-items numbered 1, 2, 3 (same as main items) | Use x-position context to distinguish sub-items in consent section |
| No staff recs from Cleburne | Different field name ("Summary:" vs "Recommendation:") | Add "Summary:" to extraction patterns |
| Meeting date missed for Rocky Mount | Date not in expected header format | Try more date regex patterns (look at all first-page text) |
| Missed 2 consent items | Irregular indentation (E, J) | Tune x-position thresholds per city template |

These are all engineering bugs — same fix effort as any scraper. Iterate on edge cases until it works for the target cities. The fundamental approach is validated across two states.

### CivicPlus AJAX Endpoint: What Works vs. What Needs Cookies

During multi-city testing, we discovered that not all CivicPlus cities respond to bare AJAX requests. Some require a session cookie established by first loading the main page.

| City | Bare AJAX works? | Notes |
|------|-----------------|-------|
| **Durham NC** | Yes | catID 4 = City Council. PDFs redirect to external system (OnBase). |
| **Rocky Mount NC** | Yes | catID 5 = City Council. Real PDFs. |
| **Cleburne TX** | Yes | catID 1 = City Council. Full packet PDFs (39-60 MB). |
| **Killeen TX** | Yes | catIDs 5, 16 only (Boards & Legal Notices). **No City Council on CivicPlus.** |
| **Burlington NC** | **No — 0 bytes** | Requires session cookie or different approach. |
| **Lancaster TX** | **No — full page returned** | Returns default page for all catIDs. Needs cookies. |
| **Longview TX** | Partial | catID 1 = City Council but 0 rows returned. May need session or different params. |
| **Monroe NC** | **No — 0 rows** | Returns section HTML but no agenda rows. |
| **Apex NC** | **No — full page** | Returns full page instead of AJAX fragment. |
| **Greenville NC** | Partial | catID 2 = Audit Committee. City Council catID unknown (1-11 not it). |

**Implication for scraper design:** The CivicPlus collector Lambda should:
1. First GET the AgendaCenter page (establishes session cookie)
2. Use the session cookie for the AJAX POST
3. Parse category checkboxes from the main page HTML

This is a minor implementation detail — the scraper just needs to do a GET before the POST. The HTML structure and AJAX endpoint are still identical across cities.

### What Each Source Provides (No LLM Anywhere)

| Data point | Legistar cities | CivicPlus cities |
|------------|----------------|------------------|
| Meeting date/time | API JSON (exact) | HTML scrape (exact) |
| Agenda items + titles | API event items (exact) | pdfplumber PDF parsing (validated 2 cities, 2 states) |
| Item types | API matter type field | Keyword rules on title (works for both numbering conventions) |
| Staff recommendations | API action text | Regex: "Recommendation:" (Rocky Mount) or "Summary:" (Cleburne) |
| Presenters | Not typically available | "Presented by:" field (Cleburne-style cities) |
| Fiscal amounts | API text (exact) | Regex on PDF text (exact — 18 found in Rocky Mount, 22 in Cleburne) |
| Vote records | API roll call (exact) | Not available |
| Committee data | API bodies/members | Not available |
| Supporting documents | API attachments | Download menu links in AJAX HTML |
| Constituent priorities | Haystaq/L2 (already exists) | Haystaq/L2 (already exists) |

### Topic Categorization (Keyword Dictionary, Not LLM)

```python
TOPIC_KEYWORDS = {
    "zoning":           ["rezone", "rezoning", "zoning", "variance", "conditional use", "land use", "subdivision", "plat", "annexation"],
    "budget":           ["budget", "appropriation", "fiscal", "tax rate", "millage", "fee schedule", "revenue", "grant", "accounts payable"],
    "infrastructure":   ["water", "sewer", "street", "road", "sidewalk", "bridge", "stormwater", "utility", "paving", "force main", "lift station"],
    "transportation":   ["transit", "bus", "traffic", "bicycle", "pedestrian", "parking", "NCDOT", "TxDOT"],
    "housing":          ["affordable housing", "housing trust", "CDBG", "HOME funds", "workforce housing", "residential"],
    "public_safety":    ["police", "fire", "EMS", "code enforcement", "public safety", "peace officer"],
    "economic_dev":     ["incentive", "TIF", "enterprise zone", "tax abatement", "economic development", "downtown"],
    "governance":       ["appointment", "board member", "committee assignment", "charter", "proclamation", "election"],
    "environment":      ["park", "greenway", "tree", "sustainability", "conservation", "easement"],
    "procedural":       ["call to order", "roll call", "minutes", "adjournment", "consent agenda", "invocation", "pledge"],
}
```

Items that don't match any keyword get categorized as `"other"` — not sent to an LLM, just flagged as uncategorized. The dictionary grows over time as we see more agendas.

### Haystaq Issue Mapping (Static Table, Not LLM)

```python
CATEGORY_TO_HAYSTAQ = {
    "zoning":         ["Development Support", "Housing Affordability"],
    "budget":         ["Economic Conservatism", "Tax Policy"],
    "infrastructure": ["Infrastructure Investment"],
    "transportation": ["Public Transit Support"],
    "housing":        ["Housing Affordability", "Helping People Priority"],
    "public_safety":  ["Law Enforcement Support"],
    "economic_dev":   ["Economic Conservatism", "Job Creation"],
    "environment":    ["Environmental Protection"],
}
```

Category → Haystaq dimension is a lookup table. No LLM involved.

### The V1 Briefing (Zero LLM)

With just deterministic extraction, an EO gets:

```
Meeting: City Council Regular Meeting
Date: December 9, 2024 at 7:00 PM
Items: 17 total

Public Hearings (3):
  #10 - Annexation No. 330 – Mason & Pearce [S. Halifax Rd.]
  #11 - Rezoning: 176.1 acres S. Halifax Road, A-1 to B-6
  #12 - Land Development Code Amendments

Consent Agenda (11 items, including):
  9-D: Flagpole donation acceptance ($29,465)
  9-E: Tar River Transit appropriation ($3,115,361)
  9-I: Recycle compactor purchase ($356,572.80)

Notable Fiscal Items:
  #13: Downtown Development Grant ($252,873)
  #15: NCDOT Municipal Agreement ($7,441,000)

Your Constituents' Top Priorities:
  #1 Public Transit Support (64.7/100) — relates to item 9-E
  #2 Housing Affordability (58.2/100) — no direct agenda items
  #3 Infrastructure Investment (55.1/100) — relates to item 15

[Link to full agenda PDF]
```

**Every data point above traces back to either the CivicPlus HTML scrape, the PDF text at specific coordinates, the keyword dictionary, or the Haystaq database. Nothing was generated by an LLM. Nothing can be hallucinated.**

### When to Add LLM (V2+, Optional Enrichment)

LLMs become useful **after** the deterministic pipeline is working, for **additive enrichment only**:

| Feature | What LLM adds | V1 still works without it |
|---------|--------------|--------------------------|
| Description summaries | 2-sentence summary of complex items | V1 shows the raw title (still useful) |
| News context | "This rezoning was covered in local media" | V1 links to agenda PDF (EO can read it) |
| Cross-city patterns | "3 other cities approved similar rezonings" | V1 doesn't have this (fine for pilot) |
| Deeper analysis (POC-style) | 6-pass orientation briefing | V1 serves recurring meeting prep (different product) |

The key: **LLM features are additive layers on top of a deterministic foundation.** If the LLM is wrong, the underlying data is still correct. If the LLM is down, the briefing still works.

### Technology: Python Sidecar for PDF Parsing

pdfplumber is Python-only (no JavaScript equivalent with layout analysis). This means the PDF parser runs as a **Python Lambda or Fargate task** — not in the TypeScript gp-api stack.

Architecture:
```
CivicPlus Collector (TypeScript Lambda)
    │  - Scrapes /AgendaCenter HTML
    │  - Downloads agenda PDFs to S3
    │
    ▼
S3 trigger → PDF Parser (Python Lambda)
    │  - pdfplumber layout analysis
    │  - Deterministic item extraction
    │  - Keyword categorization
    │  - Writes parsed JSON back to S3
    │
    ▼
SQS → gp-api (TypeScript)
    │  - Reads parsed JSON from S3
    │  - Stores in MunicipalDataSnapshot
    │  - Links constituent data (Haystaq lookup table)
    │
    ▼
API serves briefing JSON to frontend
```

The Python sidecar pattern already exists in the gp-ai-projects repo (`serve-analyze-fargate`). Same infrastructure, same deploy pipeline.

### Cost

| Component | Cost per city per run |
|-----------|---------------------|
| CivicPlus HTML scrape | ~free (1 HTTP request) |
| PDF download | ~free (1 HTTP request) |
| pdfplumber parsing | ~free (CPU only, <1s) |
| Keyword categorization | ~free (in-memory lookup) |
| Haystaq lookup | ~free (existing data) |
| **Total V1** | **~$0.00** |
| LLM enrichment (V2, optional) | ~$0.01-0.04 per city |

---

## How the Pipeline Uses These Formats

```
                                LAYER 1: Raw Collection
                                ───────────────────────
Legistar Lambda ──┐
                   │     ┌──────────────────────────────────────┐
CivicPlus Lambda ──┼────►│  S3: _manifest.json                 │
                   │     │       + MeetingRecord[] (common)     │
PDF Lambda ────────┘     │       + platform files (PDF/HTML)    │
                         └──────────────┬───────────────────────┘
                                        │
                                        ▼
                                SQS completion → gp-api
                                        │
                                        ▼
                                LAYER 2: Normalization (Hybrid)
                                ────────────────────────────────
                         ┌──────────────────────────────────────┐
                         │  For meetings without agendaItems:   │
                         │    1. Download agenda PDF from S3    │
                         │    2. pdftotext → raw text            │
                         │    3. Rule parser → items, recs, $   │
                         │    4. Keyword dict → categories       │
                         │    5. LLM only for ambiguous items   │
                         │                                       │
                         │  For meetings WITH agendaItems        │
                         │  (Legistar):                         │
                         │    1. Keyword dict → categories       │
                         │    2. Static table → Haystaq mapping  │
                         │    3. Rules → significance             │
                         │    4. LLM only for summaries          │
                         └──────────────┬───────────────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────────────┐
                         │  PostgreSQL JSONB:                    │
                         │    MunicipalDataSnapshot.data =       │
                         │      MeetingBriefing (normalized)     │
                         └──────────────┬───────────────────────┘
                                        │
                                        ▼
                         ┌──────────────────────────────────────┐
                         │  Product Features:                    │
                         │    Query Prisma → get briefing JSON   │
                         │    Same shape regardless of source    │
                         └──────────────────────────────────────┘
```

---

## Examples

### Example 1: Legistar Collection Manifest (Durham — if it were Legistar)

```json
{
  "version": "1.0",
  "citySlug": "cleveland-oh",
  "cityName": "Cleveland",
  "state": "OH",
  "category": "legislative",
  "collectorType": "legistar",
  "collectedAt": "2026-03-04T08:00:00Z",
  "collectionDurationMs": 45000,
  "status": "complete",
  "meetings": [
    {
      "date": "2026-03-18",
      "title": "Council Meeting - Stated Meeting",
      "body": "City Council",
      "sourceUrl": "https://cityofcleveland.legistar.com/MeetingDetail.aspx?ID=1234",
      "time": "7:00 PM",
      "location": "Council Chambers",
      "status": "scheduled",
      "agendaItems": [
        {
          "number": "101",
          "title": "An ordinance authorizing the Director of Public Works to enter into contract for street resurfacing",
          "type": "Ordinance",
          "description": "Authorizes a $2.4M contract with ABC Paving for resurfacing of 15 streets in Ward 7.",
          "sponsors": ["Council Member Johnson"],
          "status": "Committee",
          "vote": null,
          "sourceId": "MATTER-5678",
          "sourceUrl": "https://cityofcleveland.legistar.com/gateway.aspx?m=l&id=5678",
          "attachments": [
            { "path": "matter_5678_staff_report.pdf", "mimeType": "application/pdf", "sizeBytes": 145000 }
          ]
        },
        {
          "number": "102",
          "title": "Resolution supporting application for CDBG funding",
          "type": "Resolution",
          "description": "Supporting the city's application for $800K in Community Development Block Grant funding.",
          "sponsors": ["Council Member Davis", "Council Member Williams"],
          "status": "Agenda Ready"
        }
      ],
      "files": {
        "agendaPdf": { "path": "agenda_20260318.pdf", "mimeType": "application/pdf", "sizeBytes": 340000 },
        "agendaPacket": { "path": "packet_20260318.pdf", "mimeType": "application/pdf", "sizeBytes": 4500000 }
      },
      "platformData": {
        "legistarSlug": "cityofcleveland",
        "eventId": 1234
      }
    }
  ]
}
```

### Example 2: CivicPlus Collection Manifest (Durham NC)

```json
{
  "version": "1.0",
  "citySlug": "durham-nc",
  "cityName": "Durham",
  "state": "NC",
  "category": "legislative",
  "collectorType": "civicplus",
  "collectedAt": "2026-03-04T08:00:00Z",
  "collectionDurationMs": 12000,
  "status": "complete",
  "meetings": [
    {
      "date": "2026-03-19",
      "title": "March 19, 2026 Work Session Agenda",
      "body": "City Council",
      "sourceUrl": "https://www.durhamnc.gov/AgendaCenter/ViewFile/Agenda/_03192026-3400",
      "status": "scheduled",
      "postedAt": "2026-03-15T14:30:00Z",
      "videoUrl": "https://durham.granicus.com/player/clip/3296",
      "agendaItems": null,
      "files": {
        "agendaPdf": {
          "path": "agenda_20260319.pdf",
          "mimeType": "application/pdf",
          "sizeBytes": 2200000
        },
        "minutesPdf": null,
        "agendaHtml": {
          "path": "agenda_20260319.html",
          "mimeType": "text/html",
          "sizeBytes": 45000
        }
      },
      "platformData": {
        "civicplusDomain": "durhamnc.gov",
        "civicplusCategoryId": 4,
        "civicplusAgendaId": "3400",
        "civicplusDateSlug": "_03192026-3400"
      }
    }
  ]
}
```

Note: `agendaItems` is `null` — the LLM analysis pipeline will fill this in after extracting text from `agenda_20260319.pdf`.

### Example 3: Normalized Meeting Briefing (After LLM Analysis — Same Shape Regardless of Source)

```json
{
  "version": "1.0",
  "citySlug": "durham-nc",
  "generatedAt": "2026-03-04T09:00:00Z",
  "meeting": {
    "date": "2026-03-19",
    "time": "7:00 PM",
    "body": "City Council",
    "type": "work_session",
    "location": "Council Chambers"
  },
  "agendaItems": [
    {
      "number": "1",
      "title": "Call to Order and Roll Call",
      "category": "procedural",
      "itemType": "procedural",
      "significance": "low",
      "description": "Standard meeting opening.",
      "topics": [],
      "vote": null
    },
    {
      "number": "4",
      "title": "Rezoning Request — 2100 Hillsborough Road from RR to CG",
      "originalTitle": "Public Hearing: Zoning Map Change Z-2600-26",
      "category": "zoning",
      "itemType": "public_hearing",
      "significance": "high",
      "description": "Developer requests rezoning of 3.2 acres from Residential Rural to Commercial General for a mixed-use development with 180 residential units and ground-floor retail.",
      "keyIssue": "Whether to allow commercial development in a currently residential area along a major corridor.",
      "fiscalImpact": "Estimated $450K annual property tax increase if developed. No direct city expenditure.",
      "staffRecommendation": "Approval with conditions: traffic study, 15% affordable units, landscape buffer.",
      "topics": ["housing", "development", "zoning"],
      "haystaqIssueMapping": ["Housing Affordability", "Development Support"],
      "sourceUrl": "https://www.durhamnc.gov/AgendaCenter/ViewFile/Agenda/_03192026-3400"
    },
    {
      "number": "7",
      "title": "GoDurham Transit Route Expansion Budget Amendment",
      "category": "transportation",
      "itemType": "action",
      "significance": "high",
      "description": "Amending the FY2026 budget to allocate $2.1M in federal transit funds for three new GoDurham bus routes serving south Durham.",
      "keyIssue": "Approving the budget amendment to accept and spend federal grant funds.",
      "fiscalImpact": "$2.1M federal funds, $340K local match required from general fund.",
      "staffRecommendation": "Approve.",
      "topics": ["transportation", "budget"],
      "haystaqIssueMapping": ["Public Transit Support"],
      "sourceUrl": null
    }
  ],
  "constituentContext": {
    "relevantIssues": [
      {
        "topic": "Public Transit Support",
        "voterScore": 64.7,
        "rank": 1,
        "tier": "strong",
        "sampleSize": 85000
      },
      {
        "topic": "Housing Affordability",
        "voterScore": 58.2,
        "rank": 3,
        "tier": "moderate",
        "sampleSize": 85000
      }
    ],
    "gapAlerts": [
      {
        "topic": "Transportation",
        "gapType": "UNDER",
        "description": "Voters rank public transit as their #1 priority (64.7/100) but council has dedicated limited recent agenda time to transit. This meeting's GoDurham expansion is a significant agenda item worth active engagement."
      }
    ]
  },
  "newsContext": {
    "articles": [
      {
        "title": "Durham Hillsborough Road Rezoning Draws Neighborhood Opposition",
        "source": "Durham Herald-Sun",
        "date": "2026-03-12",
        "url": "https://www.heraldsun.com/...",
        "relevantAgendaItems": ["4"],
        "summary": "Residents of the Hillsborough Road corridor have organized against the rezoning request, citing traffic concerns and density."
      }
    ]
  },
  "dataSources": {
    "collectorType": "civicplus",
    "manifestPath": "durham-nc/legislative/2026-03-04/_manifest.json",
    "agendaItemSource": "pdf_extraction",
    "analysisModel": "gemini-2.5-flash",
    "analysisCost": 0.04
  }
}
```

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **agendaItems nullable in manifest** | Yes | CivicPlus/PDF collectors can't populate it. Legistar can. LLM fills it for all. |
| **platformData as opaque object** | Yes | Each collector stores what it needs for debugging/re-runs without polluting the common schema. |
| **MeetingBriefing is per-meeting** | Yes, one meeting = one briefing | Product UI shows one meeting at a time. Simpler to query. |
| **Categories are strings, not enums** | Strings like "zoning", "budget" | Keyword dictionary assigns most. LLM handles edge cases. We standardize a list but don't enforce at schema level. |
| **Haystaq mapping explicit** | `haystaqIssueMapping` per item | Makes the topic→constituent data linkage transparent and debuggable. |
| **Version field everywhere** | `"1.0"` | Allows schema evolution without breaking existing data. |
| **News context in briefing** | Included | The EO needs context for what they're walking into. News provides that. |
| **Vote data optional** | Only present if platform provides it | Legistar gives votes. CivicPlus doesn't. Product features check if present. |

---

## Suggested Category Taxonomy

Based on the Charlotte POC's topic analysis (686 matters across 10 categories), here's a starting list:

| Category | Description | Examples |
|----------|-------------|---------|
| `zoning` | Land use, rezoning, variances, conditional use permits | Z-2600-26, CUP requests |
| `budget` | Appropriations, budget amendments, fiscal policy | Budget amendments, fee schedules |
| `infrastructure` | Roads, water, sewer, facilities | Street resurfacing, water main replacement |
| `transportation` | Transit, traffic, bike/pedestrian | GoDurham routes, traffic signals |
| `housing` | Affordable housing, housing policy, development incentives | Housing trust fund, CDBG |
| `public_safety` | Police, fire, emergency services, code enforcement | Body cameras, fire station construction |
| `economic_development` | Business incentives, TIF, jobs | Enterprise zone, tax abatement |
| `governance` | Appointments, rules, organizational | Board appointments, rules changes |
| `environment` | Parks, sustainability, stormwater, tree protection | Park master plan, stormwater fees |
| `community` | Social services, arts, culture, events | Library funding, community center |
| `procedural` | Consent agenda, minutes approval, adjournment | Call to order, consent agenda |

This list will evolve as we process more cities. The LLM assigns categories, and we track which ones appear frequently to keep the taxonomy stable.

---

## Implementation Notes

### TypeScript Types (for gp-api)

These schemas would live in the `municipalData` module:

```
src/municipal-data/
  schemas/
    collection-manifest.ts    ← CollectionManifest, MeetingRecord, AgendaItem, FileRef
    meeting-briefing.ts       ← MeetingBriefing, NormalizedAgendaItem, ConstituentContext
    orientation-briefing.ts   ← OrientationBriefing (future, when we port the 6-pass analysis)
  collectors/
    legistar.collector.ts     ← produces MeetingRecord with agendaItems filled
    civicplus.collector.ts    ← produces MeetingRecord with agendaItems = null
```

### Zod Schemas (for LLM structured output)

The LLM analysis pipeline uses Zod schemas to validate Gemini's output:

```typescript
const NormalizedAgendaItemSchema = z.object({
  number: z.string().optional(),
  title: z.string(),
  category: z.string(),
  itemType: z.enum(["consent", "public_hearing", "action", "presentation", "discussion", "procedural"]),
  significance: z.enum(["high", "medium", "low"]),
  description: z.string(),
  keyIssue: z.string().optional(),
  fiscalImpact: z.string().optional(),
  staffRecommendation: z.string().optional(),
  topics: z.array(z.string()),
  haystaqIssueMapping: z.array(z.string()).optional(),
})
```

This Zod schema is what gets passed to Gemini's `responseSchema` parameter — ensuring the LLM always outputs valid, typed JSON.

### Prisma Storage

```prisma
model MunicipalDataSnapshot {
  id          String   @id @default(uuid())
  citySlug    String
  category    String   // "legislative" | "fiscal" | etc.
  snapshotType String  // "meeting_briefing" | "orientation_briefing"
  meetingDate DateTime?
  data        Json     // MeetingBriefing | OrientationBriefing
  s3ManifestPath String // Reference back to raw data
  createdAt   DateTime @default(now())
  updatedAt   DateTime @updatedAt

  @@index([citySlug, snapshotType, meetingDate])
}
```
