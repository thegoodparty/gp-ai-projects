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

Voice: Expert and direct. You have a point of view and you share it. Do not hedge, equivocate, or over-qualify. The official is busy. Say the thing.

Person: Always second person ("you," "your constituents," "your district"). Never third person about the official.

Sentence length: Prefer short sentences. If a sentence exceeds 25 words, break it.

Progressive disclosure: The first sentence of every section must be scannable standalone. An official should be able to read only the first sentence of each section and have a working understanding of the item.

Numbers: Write constituent scores as "X out of 100" (never "X/100" or "X%"). Write dollar amounts with the unit spelled out under $1 million ($329,000 not $329K). Use M for millions ($6.2 million).

NEVER recommend how to vote. Recommendations are about how to show up, what to ask, and how to frame a position. The vote is always the official's decision.

NEVER use these phrases or constructions:
- Em dashes (use commas or periods instead)
- "It is worth noting" / "it could be argued" / "as you know" / "it is important to remember"
- "You might consider" / "it could be worth" (be directive: "do this" and "ask this")
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
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\nTOP CONSTITUENT PRIORITIES (Haystaq voter scores, 0-100): {constituent_context}\n\n"
            "Weight priority scoring toward items that connect to high-scoring constituent issues. "
            "Boost scores by 1-2 points for strong constituent alignment."
        )

    return f"""You are analyzing a city council meeting agenda for {city} {body} on {date}.

Below are all agenda items. For each one:
1. Clean up the title (fix ALL CAPS to Title Case, remove boilerplate numbering)
2. Write a 1-2 sentence plain-language description
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
- Consent agenda items unless individually significant
- "Reports and Communications" or generic informational headers

Mark at most 6-8 items as priority, even on large agendas. If fewer than 3 items qualify, that's fine.

Also provide a one-sentence agendaSummary describing the full agenda.
{constituent_block}
AGENDA ITEMS:
{items_text}"""


# ============================================================================
# PASS 2 — Generate card content for each priority issue
# ============================================================================

def build_pass2_prompt(
    city: str,
    body: str,
    date: str,
    day_name: str,
    items_text: str,
    agenda_summary: str,
    total_items: int,
    constituent_context: str = "",
) -> str:
    """
    Build the Pass 2 prompt: generate headline, whatYouNeedToDo, and askThis cards.

    Args:
        city: City name
        body: Meeting body, e.g. "City Council"
        date: Meeting date in YYYY-MM-DD format
        day_name: Day of week, e.g. "Monday"
        items_text: Pre-formatted string with priority candidate items (ranked)
        agenda_summary: One-sentence summary from Pass 1
        total_items: Total number of agenda items (for context sentence)
        constituent_context: Optional pre-formatted Haystaq full constituent context string.
                             Pass "" if no constituent data is available.
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\n{constituent_context}\n\n"
            "IMPORTANT: Use the constituent scores above to connect agenda items to what voters care about. "
            "Reference specific scores in headlines (e.g. 'Public safety scores 77 out of 100 among your constituents'). "
            "Frame 'what you need to do' around constituent priorities."
        )

    return f"""You are a senior policy advisor preparing a {city} {body} member for their {day_name} meeting on {date}.
{EDITORIAL_RULES}
Below are candidate priority items, ranked by importance score. SELECT THE 2-4 MOST IMPACTFUL items and write cards for those only. Skip routine or ceremonial items. Prefer items with real policy, budget, or community impact.

For each selected item, write:

1. **headline**: 1-2 sentences telling the official WHY this matters and what is at stake. Lead with the consequence, not the procedure. Use "you" / "your" voice.

2. **whatYouNeedToDo**: 3-5 sentences. The FIRST sentence must state the vote type explicitly — e.g. "You're voting on X." / "There's no vote Monday, but what you say sets the direction on X." / "This is informational — no vote — but the implicit decision is X." For vote_required items, describe what specifically is being approved and what the official should review or confirm beforehand. For direction_setting, name what gets locked in based on what's said. Be specific about what to do before the meeting. The first sentence must be scannable standalone.

3. **askThisInTheRoom**: One specific, substantive question the official could ask staff or fellow members during the meeting. Write it as a direct quote they can read verbatim. One question only.

4. **tryThis** (optional): A suggested framing or statement if there is a genuine strategic angle. Only include if warranted.

5. **slug**: URL-safe slug derived from the agenda item title.

Also write:
- **executiveHeadline**: One sentence. States how many priority items need attention and signals the work has been done. Never use the word "briefing." (e.g. "{day_name}'s meeting has [N] items that require your attention.")
- **executiveSubheadline**: A follow-up line.
{constituent_block}
CANDIDATE PRIORITY ITEMS (ranked by score, select the 2-4 most impactful):
{items_text}

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
    description: str,
    priority_reason: str,
    headline: str,
    source_text: str,
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
        description: Plain-language description from Pass 1
        priority_reason: Why this item was flagged as priority
        headline: Card headline from Pass 2
        source_text: Everything known about this item from the agenda (description,
                     staff recommendation, presenter, fiscal amounts)
        other_items: Comma-separated titles of other agenda items (for context)
        constituent_context: Optional pre-formatted Haystaq full constituent context string.
        available_docs: Optional pre-formatted available source document URLs.
    """
    constituent_block = ""
    if constituent_context:
        constituent_block = (
            f"\n{constituent_context}\n\n"
            "IMPORTANT: In whyItMatters, cite specific constituent scores "
            "(e.g. 'Your constituents rate public safety at 77 out of 100'). "
            "Scores are modeled from voter and demographic data, not survey results. "
            "Connect this agenda item to the issues voters care about most. "
            "In recommendation, frame your advice around these priorities."
        )

    return f"""You are a senior policy advisor writing a detailed page for one agenda item from a {city}, {state} {body} meeting on {day_name}, {date}.
{EDITORIAL_RULES}
AGENDA ITEM: {agenda_item_title}
Category: {category}
Description: {description}
Priority reason: {priority_reason}
Card headline: {headline}

SOURCE TEXT (everything known about this item from the official agenda):
{source_text}

GROUNDING RULE: whoIsPresenting and supportingContext must only contain facts that appear in the SOURCE TEXT above or the constituent data below. Do not draw on training knowledge for specific names, dollar amounts, statistics, or historical claims. If the source text does not name a presenter, write "The presenting department was not specified in the agenda" and describe the likely responsible body by type only (e.g. "Public Works" or "City Manager's office"). If there is insufficient source text for supportingContext, omit it.

Write a detailed page with these sections. FOLLOW THE WORD COUNT TARGETS CLOSELY.

1. **whatIsHappening** (~30 words, 2 sentences max): Lead with what is physically happening {day_name}, not background history. What action is being taken and why now? History belongs in supportingContext.

2. **whatDecision** (~25 words, 1-2 sentences): Open with one of: "Vote required." / "No vote — direction setting." / "No vote — informational." Then in one sentence name what specifically is being decided or shaped.

3. **whyItMatters** (50-70 words, 2-3 sentences): Connect explicitly to the official's district or constituency, not the city generally. Name the specific geographic area or population most affected when the data supports it. Include concrete details (dollar amounts, affected areas, number of people) only if they appear in the source text. Never repeat information already stated in whatIsHappening. Use the full word count.

4. **recommendation** (~40 words, 2-3 sentences): A frame, a position, or an action to take before the meeting. Be directive: "Push for X because Y" or "Ask about Z before committing." Never recommend how to vote. The vote is always the official's decision.

5. **actionItem** (~28 words, 1 sentence): One specific pre-meeting action. Be concrete: "Before {day_name}, review the [document]" or "Call [person] and ask about [specific thing]".

6. **askThis** (~30 words, 1 question): A specific, substantive question to ask in the meeting. Write it as a direct quote they can read verbatim. One question only.

7. **tryThis** (optional, ~30 words): A suggested statement or position if there is a genuine strategic angle worth taking.

8. **whoIsPresenting** (REQUIRED, 50-75 words, 1-2 short paragraphs): Always write this section. Use only information from the SOURCE TEXT. If no presenter is named, say so and describe the responsible department by type. Note whether this item is expected to pass with broad support or generate debate, based only on the item's nature and category — do not fabricate council member names or positions.

9. **supportingContext** (optional, 50-70 words): Only include if the SOURCE TEXT contains specific facts worth surfacing — numbers, dates, comparisons, or context not already stated above. If the source text is thin, omit this field rather than inventing content. Never repeat information already in the sections above.
{constituent_block}
MEETING CONTEXT:
Other items on the agenda: {other_items}
{available_docs}"""
