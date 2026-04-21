# Pipeline Accuracy Review — Extraction → Briefing

**Date:** 2026-04-20
**Scope:** Full flow from PDF extraction through all three briefing passes
**Status:** Current state — none of items 1–7 below are yet implemented

---

## How to read this doc

Each stage gets: what it receives, what's working, what's broken. The fix table at the end is ordered by impact. Items 1–7 are all small edits and can be done in a single session.

---

## Stage 0 — PDF text extraction (PyMuPDF)

### Extraction vs. briefing see different versions of the same PDF

`extract_and_normalize.py` reads the PDF with no page markers and caps at **50,000 chars** inside the prompt.

`_load_pdf_text()` in `generate_briefing.py` reads the same PDF with `[PAGE N]` markers and caps at **100,000 chars**.

An item on page 30 of a dense packet may be entirely outside extraction's 50K window but fully visible to Pass 1, 2, and 3. The extraction description for that item is written without seeing its staff report. The Pass 1 description is written with full PDF access. These two descriptions of the same item live in the system simultaneously and are treated as equivalent.

**Fix:** Change `text[:50000]` in `prompts/extraction.py` to `text[:100000]`. Add `[PAGE N]` markers to extraction text. One line each.

---

## Stage 1 — Extraction LLM

**Receives:** PDF text (50K chars, no page markers), city, state, date.

**Produces:** Per item — `section`, `description`, `fiscal_amounts` (verbatim), `staff_recommendation`, `is_public_hearing`.

### What's working

- Temperature 0.1 is correct for a structured extraction task.
- Verbatim `fiscal_amounts` are extracted here, at the source, before any LLM paraphrase can corrupt them.
- The GROUNDING RULE ("Per staff report:" / "Per resolution text:" / "Inferred:" prefixes) is the right design intent.

### Problems

**1. The GROUNDING RULE has no enforcement mechanism.**
The prompt asks for source-prefixes in descriptions but the `description: str` Pydantic field treats all text identically. Nothing downstream checks for these prefixes or treats prefixed content differently. The rule exists only in the prompt — compliance is entirely up to the model.

**2. `description` is a required `str` — no null option.**
For procedural items (call to order, roll call, adjournment), the model must produce a description even when there's nothing substantive to say. This generates filler text that flows into the normalized file and from there into Pass 1's context.

**3. Extraction descriptions are overwritten by Pass 1.**
Extraction descriptions feed into Pass 1 items_text truncated to 200 chars. Pass 1 then writes new descriptions that replace them everywhere — in `fullAgenda` display and in Pass 3 context. The extraction descriptions exist only as a 200-char hint to Pass 1, then are discarded.

---

## Stage 2 — Pass 1: Categorize and score

**Receives:**
- Per item: title, `section` (extraction taxonomy), `fiscalAmounts`, `isPublicHearing`, `description` (200-char truncation)
- Full PDF (100K chars, page markers)
- Top 7 constituent issue names (names only — no scores, no tiers)

**Produces:** Per item — cleaned `title`, new `description`, `category`, `isPriority`, `priorityScore`, `priorityReason`.

### What's working

- Full PDF access for scoring is correct. Pass 1 can read staff reports to assess fiscal and policy impact.
- Temperature 0.1, thinking enabled for large agendas.
- The `category` taxonomy (vote_required, direction_setting, informational, public_hearing, consent, procedural) is more useful than extraction's `section` taxonomy for the briefing.

### Problems

**1. Pass 1 writes a new description for every item.**
This is the origin of the cascading summaries problem. Extraction writes a description. Pass 1 reads it as a 200-char hint, then discards it and writes a new one. Pass 1's description is what appears in `fullAgenda` and in Pass 3's context — it is one LLM hop further from the source than necessary with no auditable connection to the PDF.

**2. Two taxonomies, no mapping.**
Extraction classifies items with `section` (consent/action/public_hearing/discussion/procedural/other). Pass 1 reclassifies them with `category` (vote_required/direction_setting/informational/public_hearing/consent/procedural). These are different. An item that extraction calls `action` might become `vote_required` or `direction_setting` in Pass 1 with no record of the change. Downstream consumers see only `category`.

**3. `priorityReason` is an LLM string that flows downstream.**
Pass 1's `priorityReason` (e.g., "Large infrastructure contract serving the east side") flows into Pass 2's items_text. Pass 2 uses it to decide which passage to extract from the PDF as `sourcePassage`. If `priorityReason` contains a detail that isn't in the PDF (hallucinated by Pass 1), Pass 2 will search for — and potentially fabricate — a passage that confirms it.

**4. Chunked agendas lose the agenda summary.**
For agendas over 15 items, the pipeline chunks them and the final `agendaSummary` is assembled from counts: `"{total} agenda items including {priority_count} priority items requiring council attention."` This generic string appears in the Pass 2 prompt as context and in `fullAgendaSummary` shown to users.

---

## Stage 3 — Pass 2: Extract sourcePassage, write cards

**Receives (items_text):**
- Per item: title, `category`, vote flag emoji, `priorityReason`
- Descriptions and fiscal amounts are intentionally stripped — model must go to the PDF

**Receives additionally:**
- Full PDF (100K chars, page markers)
- Full constituent context — all tiers, all issues (not capped at top 5)
- Available doc URLs, agenda summary, total item count

**Produces:** `sourcePassage` (verbatim), `sourcePassagePage`, `headline`, `whatYouNeedToDo`, `askThisInTheRoom`, `tryThis`.

### What's working

Stripping descriptions and fiscal amounts from items_text is correct — the model is forced to go to the PDF for all claims. The requirement to write `sourcePassage` before `headline` or `whatYouNeedToDo` is the right structure.

### Problems

**1. `priorityReason` is still present in items_text.**
It's a Pass 1 LLM string, not source text. It biases which PDF passage gets selected as `sourcePassage` — steering toward confirming Pass 1's framing rather than finding the most accurate verbatim text.

**2. Constituent context passes ALL issues, not top 5.**
`format_constituent_context()` passes all Haystaq issues grouped into tiers. The `constituentData.topIssues` table in the output JSON shows only the top 5 by score. The model can reference issue #6 (e.g., environment) in a headline, but that issue won't appear in the table — so the briefing makes a claim that has no visible source in the output. This is the root cause of the QA-1 and QA-2 findings.

**3. The constituent framing instruction is unconditional.**
The prompt says: *"Frame 'what you need to do' around constituent priorities."* This applies regardless of whether the `sourcePassage` has any connection to constituent issues. A routine maintenance contract becomes "your constituents' top infrastructure concern" because infrastructure is in Haystaq, not because the sourcePassage says anything about constituents.

**4. Temperature 0.3 is wrong for verbatim transcription.**
`sourcePassage` is a copy-paste task. It should run at temperature 0.1 or lower. The QA researcher found at least one typo in a sourcePassage, suggesting the model is lightly paraphrasing. Headline writing can stay at 0.3. These are two different tasks with different temperature requirements running in the same call.

**5. No gate on sourcePassage before Pass 3.**
When `sourcePassage` is null or very short, Pass 3 runs anyway. A warning is logged but nothing stops it. The fallback sent to Pass 3 is the literal string `"No source passage available for this item."` — which becomes its stated ground truth.

---

## Stage 4 — Pass 3: Write detail page

**Receives:**
- `source_text = card.sourcePassage or "No source passage available for this item."` — verbatim anchor or literal fallback
- Full PDF (100K chars)
- `description` from Pass 1 — **LLM summary**
- `priority_reason` from Pass 1 — **LLM summary**
- `category`, `agenda_item_title`, `headline`
- Full constituent context — all tiers, all issues
- Other item titles
- Available doc URLs

**Produces:** `whatIsHappening`, `whatDecision`, `whyItMatters`, `recommendation`, `actionItem`, `askThis`, `whoIsPresenting` (optional), `supportingContext` (optional).

### What's working

- `whoIsPresenting` is now `Optional[str]` — the model can return null when no presenter is identifiable.
- The prompt correctly says to draw from the full PDF (not only sourcePassage) for presenter info.
- Fiscal validation (`check_fiscal_amounts`) correctly checks dollar amounts against the verbatim sourcePassage pool.

### Problems

**1. Pass 1 descriptions are still in the prompt. (Not yet fixed.)**
`prompts/briefing.py` lines 265–266:
```
Description: {description}
Priority reason: {priority_reason}
```
These LLM summaries sit alongside the verbatim `sourcePassage`. The model cannot distinguish which to trust. Any claim in Pass 1's description that isn't in the sourcePassage — including hallucinated details — can leak into `whyItMatters` or `whatIsHappening` undetected. This is the single highest-impact accuracy problem in the detail page.

**2. GROUNDING RULE contradicts `whoIsPresenting` optional instruction.**
The GROUNDING RULE at line 272 says:
> "If the source text does not name a presenter, write 'The presenting department was not specified in the agenda' and describe the likely responsible body by type only"

The `whoIsPresenting` field instruction at line 288 (updated 2026-04-20) says:
> "If nothing in the source text identifies who is presenting, omit this field entirely — do not guess, do not say 'not specified'"

The model encounters the GROUNDING RULE first. These instructions directly contradict each other. The GROUNDING RULE needs to be updated to match the new optional behavior.

**3. No sourcePassage gate.**
When `card.sourcePassage` is null, `source_text` becomes `"No source passage available for this item."` and Pass 3 still runs. The model generates 8 sections from the full PDF and Pass 1 descriptions with no verbatim anchor — completely ungrounded output that looks identical to a grounded briefing in the output JSON.

**4. Constituent context forces connections that may not exist.**
`prompts/briefing.py` line 251:
> "Connect this agenda item to the issues voters care about most."

This is unconditional. `whyItMatters` will include constituent framing regardless of whether `sourcePassage` supports any constituent connection. Combined with all Haystaq issues being passed (not top 5), this is what produces references to constituent concerns that the QA researcher flagged as ungrounded.

**5. `sourcePassagePage` is generated in Pass 2 but ignored in Pass 3.**
Pass 3 knows which page the sourcePassage came from. It still receives the full 100K-char PDF and must search all 60 pages for supporting details. The relevant staff report is almost always within 5 pages of `sourcePassagePage`. Windowing the PDF around that page would both reduce noise and focus the model on the right section.

**6. Temperature 0.3 for factual sections.**
`whatIsHappening` and `whatDecision` are extraction tasks — what is physically happening, what decision is being made. These should run closer to 0.1. 0.3 introduces unnecessary variation where accuracy is the only goal.

---

## Validation layer

**`check_fiscal_amounts`:** Correctly checks dollar amounts in generated output against the verbatim `sourcePassage` pool. Works as intended.

**`check_provenance`:** Checks `whoIsPresenting` and `supportingContext` for hedging language (typically/generally/usually). Too narrow — catches hedged claims but not confident fabrication. A model that writes "The city attorney will present this ordinance" with no source support is not caught.

Both checks emit warnings but do not block output. A briefing with provenance warnings ships identically to one without.

---

## Fix list — ordered by impact

| # | Issue | File | Location | Effort |
|---|---|---|---|---|
| 1 | Constituent data hard-blocks entire pipeline | `generate_briefing.py` | Lines 797–803 | 3 lines |
| 2 | GROUNDING RULE contradicts new `whoIsPresenting` optional behavior | `prompts/briefing.py` | Line 272 | 1 line edit |
| 3 | sourcePassage gate — stop Pass 3 when sourcePassage is missing | `generate_briefing.py` | Pass 3 loop | 8 lines |
| 4 | Remove Pass 1 `description` and `priorityReason` from Pass 3 prompt | `prompts/briefing.py` + `generate_briefing.py` | Lines 265–266, 512–514 | 4 lines |
| 5 | Limit constituent context to top 5 in Pass 2 and Pass 3 | `generate_briefing.py` | Lines 397, 500 | 2 lines |
| 6 | Remove "Connect this agenda item to constituent priorities" instruction | `prompts/briefing.py` | Line 251 | 1 line |
| 7 | Remove `priorityReason` from Pass 2 items_text | `generate_briefing.py` | Line 388 | 1 line |
| 8 | Equalize PDF char limit — extraction 50K → 100K | `prompts/extraction.py` | Line 62 | 1 char |
| 9 | Window PDF in Pass 3 around `sourcePassagePage` | `generate_briefing.py` | `pass3_generate_detail` | ~15 lines |
| 10 | Split Pass 2: sourcePassage at temp 0.1, cards at temp 0.3 | `generate_briefing.py` | `pass2_generate_cards` | Structural |

Items 1–7 are all targeted edits with no structural risk. Item 8 is a one-character change. Items 9–10 are moderate refactors and can follow once 1–8 are validated in production.
