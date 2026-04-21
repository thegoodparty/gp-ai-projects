"""
briefing.py — Prompt builders for the 3-pass briefing generation pipeline.

Used by scripts/generate_briefing.py to generate council meeting briefings
via Gemini structured output.

EDITORIAL_RULES is shared across Pass 2 and Pass 3.

Each pass has a corresponding build function:
  - build_pass1_prompt(): Categorize all agenda items + identify priority issues
  - build_pass2_prompt(): Generate card content for each priority issue
  - build_pass3_prompt(): Generate deep-dive detail page for one priority issue

The constituent_context and available_docs parameters are pre-formatted strings.
See generate_briefing.py: format_constituent_context() and _format_available_docs().
"""


# ============================================================================
# EDITORIAL RULES
# Shared by Pass 2 and Pass 3 prompts.
# ============================================================================

EDITORIAL_RULES = """
EDITORIAL RULES — follow these exactly:

Voice: Informational and clear. Report what the agenda and staff materials say. Do not presume to know the official's relationships, read of the room, or political constraints. Where a directive is warranted, prefer "you may want to consider" over imperative framing.

Person: Always second person ("you," "your constituents," "your district"). Never third person about the official.

Sentence length: Prefer short sentences. If a sentence exceeds 25 words, break it.

Progressive disclosure: The first sentence of every section must be scannable standalone. An official should be able to read only the first sentence of each section and have a working understanding of the item.

Numbers: Always write dollar amounts as numerals with a $ sign — never spell them out as words ("seventy-five thousand" is wrong; "$75,188" is correct). Use M for millions ($6.2 million). For amounts under $1 million, write the full numeral ($329,000 not $329K). Write constituent priorities as descriptive tiers (e.g. "strong constituent concern" or "a top priority for residents") — not as raw numeric scores.

NEVER recommend how to vote. Recommendations are about how to show up, what to ask, and how to frame a position. The vote is always the official's decision.

NEVER use these phrases or constructions:
- Em dashes (use commas or periods instead)
- "It is worth noting" / "it could be argued" / "as you know" / "it is important to remember"
- AI-sounding language: "delve," "leverage," "utilize," "in order to," "comprehensive," "robust"
- Meta-references: "we have analyzed," "our data shows," "this briefing covers"
- The word "briefing" in any generated text
"""


# ============================================================================
# PASS 1 — Categorize agenda items and identify priorities
# ============================================================================

def build_pass1_prompt(
    city: str,
    body: str,
    date: str,
    items_text: str,
    constituent_context: str = "",
    pdf_text: str = "",
) -> str:
    """
    Build the Pass 1 prompt: categorize all agenda items and score priorities.

    Args:
        city: City name
        body: Meeting body, e.g. "City Council"
        date: Meeting date in YYYY-MM-DD format
        items_text: Pre-formatted string with all agenda items (one per block)
        constituent_context: Optional pre-formatted Haystaq constituent context string.
                             Pass "" if no constituent data is available.
        pdf_text: Optional full text extracted from the agenda PDF. When provided,
                  the LLM can reference it to produce richer descriptions and more
                  accurate priority scores.
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\nCONSTITUENT PRIORITIES (modeled estimates of resident sentiment — directional, not precise):\n{constituent_context}\n\n"
            "Weight priority scoring toward items that connect to high-concern constituent issues. "
            "Boost scores by 1-2 points for strong constituent alignment."
        )

    pdf_block = ""
    if pdf_text:
        pdf_block = f"\nFULL AGENDA SOURCE DOCUMENT (use this as the authoritative reference for descriptions, dollar amounts, and fiscal details — prefer this over the structured item list when detail is richer here):\n{pdf_text}\n"

    return f"""You are analyzing a city council meeting agenda for {city} {body} on {date}.

Below are all agenda items. For each one:
1. Clean up the title (fix ALL CAPS to Title Case, remove boilerplate numbering)
2. Write a 1-2 sentence plain-language description — except for procedural items (call to order, roll call, adjournment, pledge of allegiance, invocation, approval of minutes, approval of agenda) which have no substantive content. For those, omit the description field entirely.
3. Assign a category: procedural, consent, informational, vote_required, direction_setting, or public_hearing
4. Decide if it's a genuine priority (isPriority=true) and assign a priorityScore (1-10)

Category definitions:
- procedural: Call to order, adjournment, pledge, invocation, approval of agenda, approval of minutes
- consent: Consent agenda bundles (routine approvals, minor contracts, board appointments)
- informational: Reports, updates, presentations with no action required
- vote_required: Items requiring a formal council vote (contracts, ordinances, resolutions)
- direction_setting: Discussion items that shape future decisions (budget direction, policy frameworks)
- public_hearing: Public hearings (zoning, land use, assessments)

PRIORITY SCORING — be selective. Items requiring a council VOTE are inherently higher priority than informational items because the council member must take a position. Mark isPriority=true and assign a score ONLY for items that meet these criteria:
- Score 8-10: Items requiring a vote on major policy decisions, large budget items (>$500K), new programs, land use/zoning changes, tax rate changes, utility rate changes
- Score 5-7: Items requiring a vote on significant contracts (>$100K), infrastructure projects, grant approvals, public safety programs, housing/development initiatives. Also direction-setting discussions that shape upcoming votes.
- Score 3-4: Notable appointments to policy-making bodies, intergovernmental agreements, code amendments

Informational/presentation items can be priority at most score 4-5, and only if they preview a major upcoming decision.

NEVER mark as priority:
- Condolence, congratulations, recognition, or ceremonial resolutions
- Routine board/commission appointments (unless to a body with real policy authority like a Port Authority or Planning Commission)
- Liquor license transfers or routine permit approvals
- Proclamations, designations of commemorative days
- Public comment periods (these are procedural, not a priority item)
- Approval of payment of bills, accounts payable, vouchers, or city payroll — these are routine financial approvals regardless of dollar amount
- Approval of prior meeting minutes
- Consent agenda items unless individually significant (a large dollar total in a routine bill payment or payroll approval does NOT make it individually significant)
- "Reports and Communications" or generic informational headers

Mark at most 6-8 items as priority, even on large agendas. If fewer than 3 items qualify, that's fine.

Also provide a one-sentence agendaSummary describing the full agenda.
{constituent_block}
AGENDA ITEMS:
{items_text}
{pdf_block}"""


# ============================================================================
# PASS 2A — Select priority items and extract verbatim source passages
# ============================================================================

def build_pass2a_prompt(
    city: str,
    body: str,
    date: str,
    items_text: str,
    pdf_text: str = "",
    available_docs: str = "",
) -> str:
    """
    Build the Pass 2a prompt: select the 2-4 most impactful items and extract
    verbatim source passages for each. Runs at temperature 0.1 — this is a
    transcription task, not a writing task.

    Args:
        city: City name
        body: Meeting body, e.g. "City Council"
        date: Meeting date in YYYY-MM-DD format
        items_text: Pre-formatted string with priority candidate items (ranked)
        pdf_text: Full text extracted from the agenda PDF (with [PAGE N] markers).
        available_docs: Optional pre-formatted available source document URLs.
    """
    pdf_block = ""
    if pdf_text:
        pdf_block = f"\nFULL AGENDA SOURCE DOCUMENT:\n{pdf_text}\n"

    return f"""You are extracting source text from a {city} {body} agenda packet for the meeting on {date}.

Below are candidate priority items, ranked by importance score. Do two things:

1. SELECT THE 2-4 MOST IMPACTFUL items. Skip routine, ceremonial, or purely procedural items. Prefer items with real policy, budget, or community impact.

2. For each selected item, extract ALL relevant source sections as a labeled list. Each section should be copied verbatim, character-for-character, with no changes, no summarizing, no paraphrasing.

SECTIONS TO EXTRACT (include all that are present in the document for this item):
- **Staff Memo** — the cover memo or transmittal letter, including the full TO:/FROM:/SUBJECT:/DATE: header lines. The header identifies the author and presenter.
- **Resolution Text** or **Ordinance Text** — the formal legislative language (WHEREAS clauses, NOW THEREFORE, SECTION 1, etc.)
- **Staff Report** — the narrative analysis or background report if present
- **Financial Schedule** — any table or schedule of dollar amounts, appropriations, or budget line items
- **Exhibit** — key exhibits (easement terms, contract terms) if they contain material facts not in the resolution

EXTRACTION RULES:
- Copy each section character-for-character. This is a transcription task, not a writing task.
- Assign a short descriptive label to each section (e.g. "Staff Memo", "Resolution Text", "Financial Schedule", "Exhibit A"). For prior meeting minutes, use a date-specific label like "April 7 Meeting Minutes".
- Record the [PAGE N] number for each section.
- Exclude boilerplate signature/attestation blocks: "PASSED:", "APPROVED:", "ATTEST:", "AYES:", "NAYS:", "Mayor", "Clerk of Council", blank signature lines.
- If a section is absent from the document (e.g. no staff memo, no financial schedule), skip it — do not fabricate.
- The first section in your list should be the most substantive (usually Staff Memo or Resolution Text).

PRIOR MEETING MINUTES — set is_prior_minutes=true when a section is narrative from a prior meeting (past-tense language: "presented", "motion passed 5-0", "no action was taken", "councilmember X seconded by Y moved to…"). These sections are VALUABLE — include them — but must be flagged so the briefing writer knows the context.
- Forward-looking sections (staff memos, resolutions, staff reports, exhibits) → is_prior_minutes=false
- Prior meeting minutes embedded in the packet → is_prior_minutes=true
- An item may have a mix: e.g. a staff memo (is_prior_minutes=false) plus a prior vote record showing council already debated this (is_prior_minutes=true)

Also record:
- **sourcePassagePage**: the [PAGE N] number of the first (primary) section
- **sourceDocUrl**: the URL of the source document if listed in AVAILABLE SOURCE DOCUMENTS
{available_docs}
CANDIDATE PRIORITY ITEMS (ranked by score):
{items_text}
{pdf_block}"""


# ============================================================================
# PASS 2B — Write card content using extracted source passages as grounding
# ============================================================================

def build_pass2b_prompt(
    city: str,
    body: str,
    date: str,
    day_name: str,
    passages_text: str,
    agenda_summary: str,
    total_items: int,
    constituent_context: str = "",
) -> str:
    """
    Build the Pass 2b prompt: write headline, whatYouNeedToDo, and askThis for
    each selected item, using the verbatim source passages extracted in Pass 2a
    as the ground truth. Runs at temperature 0.3.

    Args:
        city: City name
        body: Meeting body, e.g. "City Council"
        date: Meeting date in YYYY-MM-DD format
        day_name: Day of week, e.g. "Monday"
        passages_text: Pre-formatted string with selected items and their verbatim passages
        agenda_summary: One-sentence summary from Pass 1
        total_items: Total number of agenda items
        constituent_context: Optional pre-formatted Haystaq constituent context string.
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\n{constituent_context}\n\n"
            "IMPORTANT: Use the constituent scores above to inform your framing and prioritization — "
            "but do NOT cite scores or percentages in headlines. "
            "Only reference a constituent issue when one of the issue names listed above appears explicitly in the sourcePassage. "
            "Do not describe an item as addressing constituent concerns unless you can name a specific issue from the list. "
            "If none of the listed issues connect to the sourcePassage, write the headline and whatYouNeedToDo without any constituent framing."
        )

    return f"""You are a senior policy advisor preparing a {city} {body} member for their {day_name} meeting on {date}.
{EDITORIAL_RULES}
Below are selected priority items with their verbatim source passages already extracted. Write card content for each item. Base all specific claims on the sourcePassage provided — do not introduce facts from other sources.

SOURCE TYPE RULE — check is_prior_minutes on each section:
- If any section labeled "Staff Memo", "Staff Report", "Resolution Text", or "Ordinance Text" has is_prior_minutes=false: write normally in future tense — this is genuine upcoming business.
- If ALL narrative sections (Staff Memo, Staff Report, Resolution Text) have is_prior_minutes=true — even if a Financial Schedule is also present — the substantive source is prior meeting minutes only. Write to reflect what ALREADY happened and clarify Tuesday's role (formal approval of those minutes, follow-up, or informational). Do NOT write "You are asked to approve X" or "You will vote on X" when X already passed at a prior meeting. Example: "At the April 7 meeting, council voted 5-0 to authorize Bolton and Menk. Tuesday's item is the formal approval of those minutes."
- If an item has a true mix (forward-looking staff memo + prior minutes): lead with the staff memo content and reference the prior vote as context.

For each item write:

1. **headline**: One punchy sentence, max 15 words. Captures what's at stake and what this meeting means. Do NOT include constituent scores, percentages, or numeric rankings.

2. **whatYouNeedToDo**: 3-5 sentences. The FIRST sentence must state the vote type explicitly — e.g. "You're voting on X." / "There's no vote {day_name}, but what you say sets the direction on X." For vote_required items, describe what specifically is being approved and what to review beforehand. The first sentence must be scannable standalone. Base all specific claims on the sourcePassage. If all sources are prior minutes, the first sentence should reflect Tuesday's actual role (e.g. "You're voting to approve the April 7 minutes, which record council's 5-0 decision to authorize X.").

3. **askThisInTheRoom**: One specific, substantive question the official could ask in the meeting. Write it as a direct quote they can read verbatim.

4. **slug**: URL-safe slug derived from the agenda item title.

Also write:
- **executiveHeadline**: One sentence. States how many priority items need attention. Never use the word "briefing." (e.g. "{day_name}'s meeting has [N] items that require your attention.")
- **executiveSubheadline**: A follow-up line.
{constituent_block}
SELECTED ITEMS WITH SOURCE PASSAGES:
{passages_text}

FULL AGENDA CONTEXT:
The meeting has {total_items} total items. Summary: {agenda_summary}"""


# ============================================================================
# PASS 3 — Generate deep-dive detail page for one priority issue
# ============================================================================

def build_pass3_prompt(
    city: str,
    state: str,
    body: str,
    date: str,
    day_name: str,
    agenda_item_title: str,
    category: str,
    headline: str,
    source_sections: str,
    other_items: str,
    constituent_context: str = "",
    available_docs: str = "",
) -> str:
    """
    Build the Pass 3 prompt: deep-dive detail page for a single priority issue.

    Args:
        city, state, body, date, day_name: Meeting metadata
        agenda_item_title: Title of the priority issue being detailed
        category: Category from Pass 1 (vote_required, direction_setting, etc.)
        headline: Card headline from Pass 2
        source_sections: Pre-formatted string of labeled source sections extracted in
                         Pass 2a — the sole ground truth for all claims. Each section
                         is labeled (e.g. [Staff Memo], [Resolution Text]) and verbatim.
        other_items: Comma-separated titles of other agenda items (for context)
        constituent_context: Optional pre-formatted Haystaq full constituent context string.
        available_docs: Optional pre-formatted available source document URLs.
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\n{constituent_context}\n\n"
            "IMPORTANT: These are modeled estimates of constituent sentiment — directional, not precise. "
            "In whyItMatters, describe constituent priorities using tier language "
            "(e.g. 'Residents show strong concern for infrastructure investment') rather than citing raw numeric scores. "
            "Only reference a constituent issue when one of the issue names listed above appears explicitly in the source sections or agenda item. "
            "Do not describe an item as addressing constituent concerns unless you can name a specific issue from the list. "
            "If none of the listed issues connect to the source sections, write whyItMatters without any constituent framing."
        )

    return f"""You are a senior policy advisor writing a detailed page for one agenda item from a {city}, {state} {body} meeting on {day_name}, {date}.
{EDITORIAL_RULES}
AGENDA ITEM: {agenda_item_title}
Category: {category}
Card headline: {headline}

SOURCE SECTIONS (verbatim extracts from the agenda packet — these are the sole ground truth for all claims):
{source_sections}

SOURCE TYPE RULE — check is_prior_minutes on each section:
- If any section labeled "Staff Memo", "Staff Report", "Resolution Text", or "Ordinance Text" has is_prior_minutes=false: that is genuine forward-looking agenda material. Use it as the primary ground truth and write in future tense.
- If ALL narrative sections (Staff Memo, Staff Report, Resolution Text) have is_prior_minutes=true — even if a Financial Schedule or Exhibit is present — treat this item as prior-minutes-only: write to reflect what already happened and what Tuesday's role actually is (formal approval of minutes, follow-up discussion, or informational). In whatIsHappening, say what was already decided and that Tuesday's action is approving those minutes. In whatDecision, say "Vote required. Council is voting to approve the [date] minutes, which record the [X] decision." Do NOT write "You are asked to approve X" or "You are asked to formally approve X" when X already passed at a prior meeting.
  - CRITICAL: If the minutes say "No action was taken" for this item, do NOT say the minutes "record a decision" — they record a discussion with no outcome. Use "No vote, informational. The [date] minutes note staff presented [topic]; no council action was taken."

GROUNDING RULE: Every factual claim in every field must be traceable to a word or phrase in the SOURCE SECTIONS above. Do not draw on training knowledge for specific names, dollar amounts, statistics, addresses, parcel numbers, or historical claims. If a fact does not appear in the source sections, do not state it. There is no other document to consult — what is in the source sections is all you have.

DOLLAR AMOUNT RULE: Dollar amounts may only appear in any field if they appear verbatim in the SOURCE SECTIONS above. Do not infer, round, compute, or paraphrase amounts.

ADDRESS AND NAME RULE: Street addresses, parcel numbers (PPN, APN, etc.), and individual person names may only appear if they appear verbatim in the SOURCE SECTIONS above.

Write a detailed page with these sections. FOLLOW THE WORD COUNT TARGETS CLOSELY.

1. **whatIsHappening** (~30 words, 2 sentences max): Lead with what is physically happening {day_name}, not background history. What action is being taken and why now? History belongs in supportingContext.

2. **whatDecision** (~25 words, 1-2 sentences): Open with one of these exact phrases (no dashes, no line breaks, no variations): "Vote required." / "No vote, direction setting." / "No vote, informational." Then in one sentence name what specifically is being decided or shaped. IMPORTANT: If the agenda item title or source text includes the word "Action" (e.g. "Discussion and Action," "Discussion and Possible Action"), use "Vote required." — do not use "No vote" phrases when action is explicitly listed.

3. **whyItMatters** (50-70 words, 2-3 sentences): Connect explicitly to the official's district or constituency, not the city generally. Name the specific geographic area or population most affected when the data supports it. Include concrete details (dollar amounts, affected areas, number of people) only if they appear in the source sections. Never repeat information already stated in whatIsHappening. Use the full word count.

4. **recommendation** (~40 words, 2-3 sentences): A frame for how to think about this decision — what questions to weigh, what trade-offs to understand. Draw from the staff materials in the source sections. Do not assign tasks or directives here. Never recommend how to vote. The vote is always the official's decision.

5. **actionItem** (~28 words, 1 sentence): One specific, concrete, pre-meeting task — name the exact document to read, the person to call, or the specific thing to verify. Not general framing. Be concrete: "Before {day_name}, review the [document]" or "Call [person] and ask about [specific thing]".

6. **askThis** (~30 words, 1 question): A specific, substantive question to ask in the meeting. Write it as a direct quote they can read verbatim. One question only.

7. **whoIsPresenting** (optional, 50-75 words, 1-2 short paragraphs): Include this only if a presenter or responsible department is explicitly named in the SOURCE SECTIONS above — typically in a Staff Memo FROM: line or a staff report byline. Write only what the source says: name and title. Do not add "is responsible for presenting," "will be presenting," "will handle," or any inference language. STRICT RULE: If you find yourself writing "likely," "probably," "may be," "presumably," "responsible for," or "is expected to" — stop and omit the field instead. CRITICAL: If the source contains "Presentation: No" (or similar wording indicating no live presentation), omit this field entirely even if a "Presented by:" name appears — that name is the document author, not a presenter.

8. **supportingContext** (optional, 50-70 words): Only include if the SOURCE SECTIONS contain specific facts worth surfacing — numbers, dates, comparisons, or context not already stated above. Every sentence must be directly traceable to a word or phrase in the source sections. When in doubt, omit. Never repeat information already in the sections above.

{constituent_block}
MEETING CONTEXT:
Other items on the agenda: {other_items}
{available_docs}"""
