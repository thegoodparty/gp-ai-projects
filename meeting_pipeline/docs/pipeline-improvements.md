# Pipeline Improvements

**Date:** 2026-04-20
**Scope:** Extraction through all three briefing passes

---

## 1. Fix the GROUNDING RULE contradiction in Pass 3

`prompts/briefing.py` line 272 still contains the old instruction: "write 'The presenting department was not specified in the agenda' and describe the likely responsible body by type only." This directly contradicts the updated `whoIsPresenting` instruction at line 288, which says to omit the field entirely when no presenter is identifiable. The model sees both instructions. Remove the fallback sentence from line 272 so the grounding rule is consistent with the field instruction.

---

## 2. Add a sourcePassage gate before Pass 3

When `sourcePassage` is null or under 80 characters, skip Pass 3 for that item. Currently the fallback is the literal string `"No source passage available for this item."` and Pass 3 runs anyway, generating 8 sections with no verbatim anchor. A card without a detail page is more honest than a detail page built on nothing.

---

## 3. Remove Pass 1 descriptions and priorityReason from Pass 3

`prompts/briefing.py` lines 265–266 pass Pass 1's LLM-generated `description` and `priorityReason` into Pass 3 alongside the verbatim `sourcePassage`. The model cannot distinguish which source to trust and synthesizes from both. Any claim that originated in Pass 1's description — including hallucinated details — can appear in `whatIsHappening` or `whyItMatters` without detection. Remove these two fields from the Pass 3 prompt and stop passing them from `pass3_generate_detail()`. Pass 3 has the verbatim sourcePassage and the full PDF — that is sufficient. Once this change and item 8 are both implemented, `priorityReason` has no remaining consumer and can be removed from the `CategorizedAgendaItem` schema.

---

## 4. Remove the constituent data hard block

`generate_briefing.py` lines 797–803 skip the entire briefing when Haystaq data is absent. Constituent context should enhance a briefing, not gate it. Cities with complete agenda data should receive a briefing regardless of Haystaq coverage. Make constituent data a genuine optional enhancement — when absent, the pipeline proceeds without constituent framing.

---

## 5. Make constituent framing conditional on sourcePassage connection

Pass 2 and Pass 3 prompts currently instruct the model to "frame whatYouNeedToDo around constituent priorities" and "connect this agenda item to the issues voters care about most" — unconditionally. This produces constituent claims that have no basis in the sourcePassage. Constituent priorities should only appear in briefing text when the sourcePassage or agenda item explicitly connects to them. If no connection exists in the source, do not manufacture one.

---

## 6. Limit constituent context to top 5 in Pass 2 and Pass 3

`format_constituent_context()` passes all Haystaq issues to the model. The `constituentData.topIssues` table in the output JSON shows only the top 5. The model can reference issue #6 in briefing text but the reader cannot verify it — there is no corresponding entry in the output. Pass the model the same set it is permitted to cite.

---

## 7. Name specific claim types that require verbatim grounding in Pass 3

The current grounding rule in Pass 3 is generic. The most harmful errors are specific: wrong dollar amounts, wrong vote type, wrong presenter names, wrong ordinance or resolution numbers, wrong meeting dates. These are the claims that most directly damage an official's trust if incorrect. Add explicit instruction naming these: "Dollar amounts, vote type, ordinance and resolution numbers, presenter names, and dates must appear verbatim in the sourcePassage or full PDF. Do not infer, round, or paraphrase these."

---

## 8. Remove priorityReason from Pass 2 items_text

`priorityReason` is a Pass 1 LLM string that steers which PDF passage gets selected as `sourcePassage`. If Pass 1 hallucinated a detail in its reason, Pass 2 will search for that detail in the PDF and may select the wrong passage or fabricate one that confirms it. Pass 2 has the full PDF and does not need LLM interpretation to find the relevant passage. Remove `priorityReason` from the items_text block passed to Pass 2. Once this change and item 3 are both implemented, `priorityReason` has no remaining consumer and can be removed from the `CategorizedAgendaItem` schema.

---

## 9. Lower temperature for sourcePassage extraction

`sourcePassage` is a transcription task. Running it at temperature 0.3 alongside headline writing produces light paraphrasing — confirmed by the QA researcher finding a typo in extracted source text. Split Pass 2 into two calls: sourcePassage extraction at temperature 0.1, card writing (headline, whatYouNeedToDo, askThisInTheRoom) at temperature 0.3. These are different tasks and need different temperatures.

---

## 10. Equalize PDF char limits and page markers between extraction and briefing

Extraction truncates at 50,000 chars; all briefing passes see 100,000 chars. Items in the back half of a dense packet have thin extraction descriptions because their staff reports fall beyond the 50K window. This produces weaker normalized data for those items. Two changes: (1) Change the limit in `prompts/extraction.py` to 100,000. (2) Add `[PAGE N]` markers to `extract_pdf_text()` in `extract_and_normalize.py` — currently it joins raw page text with no markers, while `_load_pdf_text()` in `generate_briefing.py` adds `[PAGE N]` before each page. Both functions should produce the same format.

---

## 11. Window the Pass 3 PDF around sourcePassagePage

Pass 3 knows which page the sourcePassage came from (`sourcePassagePage`, populated by Pass 2) but receives the full 100,000-char PDF anyway. The relevant staff report is almost always within a few pages of the sourcePassage page. Narrow the PDF context in Pass 3 to a window around that page. This reduces the chance the model pulls in material from unrelated items' staff reports and focuses its attention on the right section.

---

## 12. Stop Pass 1 from rewriting descriptions

Extraction writes a description for each item. Pass 1 reads that description as a 200-char hint, then discards it and writes a new one from scratch. Pass 1's descriptions are what appear in the `fullAgenda` display. This means the display uses descriptions that are one LLM hop further from the source than necessary, and there is no auditable connection between the displayed description and the PDF. Once the PDF char limits are equalized (item 10), evaluate whether extraction descriptions are accurate enough to use directly in `fullAgenda`, removing the rewrite step from Pass 1.

---

## 13. Unify the section/category taxonomy

Extraction classifies items using `section` (consent, action, public_hearing, discussion, procedural, other). Pass 1 reclassifies them using `category` (vote_required, direction_setting, informational, public_hearing, consent, procedural). These are different taxonomies applied to the same items with no record of the mapping. Update the extraction prompt and `AgendaItem` schema to use the Pass 1 taxonomy so items enter the pipeline already classified correctly and Pass 1 can focus on scoring and prioritization rather than reclassification. Depends on item 12: evaluate taxonomy unification only after extraction descriptions are accurate enough to trust directly — if Pass 1 is still rewriting descriptions, classification will still need a reclassification step.

---

## 14. Sharpen the distinction between recommendation and actionItem

The `recommendation` and `actionItem` fields in Pass 3 produce similar content in practice. Both currently ask the model to tell the official what to do before the meeting. Tighten the prompt contrast so each field does a distinct job: `recommendation` is a frame for how to think about the decision — what questions to weigh, what trade-offs to understand, not a task or directive. `actionItem` is one specific, concrete, time-bounded task — name a document to read, a person to call, or a specific thing to verify, not general framing.

---

## 15. Fix chunked agendas losing the agenda summary

For agendas over 15 items, Pass 1 runs in chunks and the final `agendaSummary` is assembled from a template: "{total} agenda items including {priority_count} priority items requiring council attention." This generic string appears in the Pass 2 prompt and in `fullAgendaSummary` shown to users. After chunking completes, run a lightweight follow-up call that receives the full list of categorized items and generates a real one-sentence summary from them.

---

## 16. Enforce the extraction GROUNDING RULE

The extraction prompt asks the model to prefix inferred or secondary-source content with "Per staff report:" or "Inferred:" in descriptions. The `description: str` Pydantic field treats all text identically — nothing downstream checks for these prefixes or acts on them differently. Enforce the rule: after extraction, parse each description for these prefixes and set a boolean `isInferred` flag on the `AgendaItem`. Downstream code (Pass 1, display) can then treat inferred items differently. Do not remove the rule — unenforced instructions add noise, but the right fix is enforcement, not removal.

---

## 17. Instruct extraction to omit descriptions for procedural items

`AgendaItem.description` is already `str | None = None` in `extract_and_normalize.py` — the schema change is done. The remaining change is in the prompt: `prompts/extraction.py` never explicitly tells the model to omit descriptions for procedural items. For procedural items (call to order, roll call, adjournment, approval of minutes) with no staff report or background section, the model currently generates filler text because it has no instruction to return null. Add explicit instruction: omit description for procedural items with no substantive staff material.

---

## 18. Broaden the provenance check beyond hedging language

`check_provenance()` scans `whoIsPresenting` and `supportingContext` for hedging words (typically, generally, usually, historically, often). This catches cautious fabrication but misses confident fabrication entirely. A model that writes "The city attorney will present this ordinance" with no source support passes the check because there is no hedge word. Extend the check to flag named persons and specific roles in `whoIsPresenting` and `supportingContext` that do not appear verbatim in the full PDF text. Note: `whoIsPresenting` is explicitly allowed to draw from the full PDF (not only `sourcePassage`), so the validation scope must be the full PDF — not just `sourcePassage`. Additionally, the current `check_provenance()` has a structural bug: it tries to look up source text by `detail.agendaItemTitle`, but `PriorityIssueDetail` has no such field, so `source` is always `""`. The hedging-language check still works because it only reads sentence content — but when extending to check named persons against source text, the function must receive `pdf_text` directly as a parameter rather than relying on the broken title lookup.
