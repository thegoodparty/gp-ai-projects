"""
generate_briefing.py — Generate council meeting briefings from normalized meeting data.

Takes normalized meeting JSON from output/normalized/ and produces a full briefing
JSON with executive summary, priority issue cards, detail pages, and constituent data.

If Haystaq constituent data is available (issue_scores.json in sources/{city-slug}/constituent/),
the pipeline uses it to weight priority selection and inject real constituent scores.

Pipeline:
  Pass 1: Categorize all agenda items + identify priority issues
  Pass 2: Generate card content for each priority issue (headline, action, questions)
  Pass 3: Generate detail content for each priority issue (deep analysis)

Storage:
    Reads/writes via STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.

Usage:
    # Generate briefing for a specific normalized meeting storage key
    uv run python meeting_pipeline/scripts/generate_briefing.py --file meeting_pipeline/output/normalized/johnstown-OH_2026-04-07.json

    # Generate for a city slug (most recent normalized meeting)
    uv run python meeting_pipeline/scripts/generate_briefing.py --city johnstown-OH

    # Generate for all normalized meetings
    uv run python meeting_pipeline/scripts/generate_briefing.py --batch

    # Dry run
    uv run python meeting_pipeline/scripts/generate_briefing.py --city johnstown-OH --dry-run

Output:
    {output_prefix}/briefings/{city-slug}_{date}_briefing.json
"""

import json
import re
import time
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from meeting_pipeline.prompts.briefing import (
    build_pass1_prompt,
    build_pass2a_prompt,
    build_pass2b_prompt,
    build_pass3_prompt,
)
from meeting_pipeline.shared.config import AgentConfig

# ============================================================================
# FORMAT ADAPTER — normalized JSON → meeting dict used by generation pipeline
# ============================================================================

def normalized_to_meeting_dict(normalized: dict) -> dict:
    """
    Convert our output/normalized/{city}_{date}.json format to the meeting dict
    format expected by the briefing generation pipeline.

    Normalized format:
        official.city, official.state, meeting.city_slug, meeting.body,
        meeting.date, meeting.time, meeting.platform,
        sources.platform_meeting_url, sources.agenda_files,
        agenda.items[].{title, section, description, fiscal_amounts,
                        is_public_hearing, staff_recommendation}

    Meeting dict format (what generate_briefing pipeline uses):
        citySlug, cityName, state, body, date, time, sourceUrl, sourceType,
        data.agendaItems[].{title, section, fiscalAmounts, isPublicHearing,
                             description, staffRecommendation, attachments}
    """
    official = normalized.get("official", {})
    meeting = normalized.get("meeting", {})
    sources = normalized.get("sources", {})
    agenda = normalized.get("agenda", {})

    # Convert agenda items from snake_case to camelCase
    agenda_items = []
    for item in agenda.get("items", []):
        agenda_items.append({
            "number": item.get("number"),
            "title": item.get("title", ""),
            "section": item.get("section"),
            "description": item.get("description"),
            "fiscalAmounts": item.get("fiscal_amounts", []),
            "isPublicHearing": item.get("is_public_hearing", False),
            "staffRecommendation": item.get("staff_recommendation"),
            "attachments": [],  # URLs are in sources.agenda_files, not per-item
        })

    # Build supporting docs from agenda_files
    # Keep storage_pdf entries (needed by _load_pdf_text) but exclude local_pdf
    agenda_files = [
        {"name": f.get("name", ""), "type": f.get("type", ""), "url": f.get("url", "")}
        for f in sources.get("agenda_files", [])
        if f.get("type") != "local_pdf" and f.get("url")
    ]

    return {
        "citySlug": meeting.get("city_slug", ""),
        "cityName": official.get("city", ""),
        "state": official.get("state", ""),
        "body": meeting.get("body", "City Council"),
        "date": meeting.get("date", ""),
        "time": meeting.get("time"),
        "sourceUrl": sources.get("platform_meeting_url", ""),
        "sourceType": meeting.get("platform", ""),
        "agendaFiles": agenda_files,
        "data": {
            "agendaItems": agenda_items,
        },
    }


# ============================================================================
# HAYSTAQ CONSTITUENT DATA
# ============================================================================

def load_constituent_data(city_slug: str, storage, sources_prefix: str) -> dict | None:
    """Load Haystaq constituent data for a city if available."""
    key = f"{sources_prefix}/{city_slug}/constituent/issue_scores.json"
    if not storage.exists(key):
        failure_key = f"{sources_prefix}/{city_slug}/constituent/haystaq_failure.json"
        if storage.exists(failure_key):
            failure = storage.read_json(failure_key)
            print(f"  ⚠ Haystaq data missing for {city_slug}: {failure.get('reason')} — {failure.get('error', '')}")
        return None
    data = storage.read_json(key)
    return data if data.get("issues") else None


def format_constituent_context(constituent: dict, n: int | None = None) -> str:
    """Format constituent data into a text block for LLM prompts using tiered prose.

    Args:
        constituent: Haystaq constituent data dict.
        n: If set, limit to the top N issues by score. Pass n=5 for Pass 2 and Pass 3
           so the model only references issues that appear in constituentData.topIssues output.
    """
    issues = constituent.get("issues", [])
    if not issues:
        return ""

    if n is not None:
        issues = sorted(issues, key=lambda i: i.get("score", 0), reverse=True)[:n]

    lines = ["CONSTITUENT PRIORITIES (modeled estimates of resident sentiment — directional, not precise):"]
    voter_count = constituent.get("voter_count_with_scores", 0)
    if voter_count:
        lines.append(f"  Based on {voter_count:,} registered voters")

    tiers = {"Critical": [], "Strong": [], "Moderate": [], "Lower": []}
    for issue in issues:
        tier = issue.get("tier_label", "Lower")
        tiers.setdefault(tier, []).append(issue)

    tier_prose = {
        "Critical": "top priorities for residents",
        "Strong": "strong concern among residents",
        "Moderate": "moderate concern among residents",
        "Lower": "lower-priority issues for residents",
    }

    for tier_name in ["Critical", "Strong", "Moderate", "Lower"]:
        tier_issues = tiers.get(tier_name, [])
        if tier_issues:
            issue_names = ", ".join(i["name"] for i in tier_issues)
            lines.append(f"  - {tier_prose[tier_name]}: {issue_names}")

    return "\n".join(lines)


def format_top_constituent_issues(constituent: dict, n: int = 5) -> str:
    """Format just the top N constituent issues as a compact summary."""
    issues = constituent.get("issues", [])
    if not issues:
        return ""
    top = sorted(issues, key=lambda i: i.get("score", 0), reverse=True)[:n]
    return ", ".join(i["name"] for i in top)


# ============================================================================
# PYDANTIC MODELS — LLM STRUCTURED OUTPUT
# ============================================================================

class SourceCitation(BaseModel):
    field: str = Field(description="The field this citation supports (e.g. 'fiscal_amounts', 'vote_type', 'description', 'whatIsHappening', 'whyItMatters')")
    quote: str = Field(description="Verbatim sentence or clause from the source document — exact wording, not paraphrased")


class CategorizedAgendaItem(BaseModel):
    originalTitle: str = Field(description="Original title from the agenda")
    title: str = Field(description="Clean, readable title (fix ALL CAPS, remove numbering prefixes)")
    description: str | None = Field(None, description="1-2 sentence plain-language description of what this item is about. Omit entirely for procedural items (call to order, roll call, adjournment, pledge, invocation, approval of minutes) — do not generate filler content for items with nothing substantive to describe.")
    category: str = Field(description="One of: procedural, consent, informational, vote_required, direction_setting, public_hearing")
    isPriority: bool = Field(False, description="True if this item is a genuine policy decision, significant spending, or high public interest")
    priorityScore: int = Field(0, description="0 if not priority. 1-10 if priority: 10=highest impact (major policy/budget), 1=lowest (routine vote). Only score items where isPriority=true.")


class AgendaCategorization(BaseModel):
    items: list[CategorizedAgendaItem]
    agendaSummary: str = Field(description="One-sentence summary of the full agenda (e.g. '12 items including votes, direction-setting discussions, and procedural business.')")


# ── Pass 2 split schemas ──────────────────────────────────────────────────────
# Pass 2a (temp 0.1) extracts verbatim source sections (multiple labeled excerpts per item).
# Pass 2b (temp 0.3) writes card text using those sections as grounding.
# Results are merged back into BriefingCards for downstream compatibility.

class SourceSection(BaseModel):
    label: str = Field(description="Short label for this section, e.g. 'Staff Memo', 'Resolution Text', 'Financial Schedule', 'Exhibit A'. If from prior meeting minutes, use a date-specific label like 'April 7 Meeting Minutes'.")
    text: str = Field(description="Verbatim text copied character-for-character from this section of the document. No changes, no summarizing.")
    page: int | None = Field(None, description="Page number from [PAGE N] markers where this section was found")
    is_prior_minutes: bool = Field(False, description="True if this section comes from prior meeting minutes embedded in the packet (past-tense narrative: presented, motion passed, no action was taken, etc.) rather than from forward-looking agenda materials (staff memos, resolutions, staff reports).")


class PriorityIssueCard(BaseModel):
    agendaItemTitle: str = Field(description="Title of the underlying agenda item")
    slug: str = Field(description="URL-safe slug (e.g. 'public-safety-camera-expansion')")
    sourcePassage: str | None = Field(None, description="Primary verbatim source text (first section). Kept for backward compatibility.")
    sourceSections: list[SourceSection] = Field(default_factory=list, description="All labeled source sections extracted for this item.")
    sourcePassagePage: int | None = Field(None, description="Page number (from [PAGE N] markers in the document) where the primary source section was found.")
    sourceDocUrl: str | None = Field(None, description="URL of the source document this passage came from, if multiple documents are listed in AVAILABLE SOURCE DOCUMENTS. Use the exact URL from that list.")
    headline: str = Field(description="One punchy sentence, max 15 words. What's at stake for constituents and what this meeting means — in a single breath. No scores, percentages, or numeric rankings.")
    whatYouNeedToDo: str = Field(description="Actionable paragraph: what the council member should do about this item, what's being decided, what to watch for. Base all claims on sourcePassage, not on the item description above.")
    askThisInTheRoom: str = Field(description="A specific question the council member could ask during the meeting")
    tryThis: str | None = Field(None, description="Optional: a suggested statement or talking point the council member could use")


class BriefingCards(BaseModel):
    executiveHeadline: str = Field(description="Executive summary headline (e.g. 'Monday's meeting has 3 items that directly connect to your platform.')")
    executiveSubheadline: str = Field(description="Follow-up line (e.g. 'Here's what requires your attention and what to do about each one:')")
    priorityIssues: list[PriorityIssueCard] = Field(description="2-4 priority issues, ordered by importance")


class SourcePassageItem(BaseModel):
    agendaItemTitle: str = Field(description="Title of the agenda item — must match a candidate item exactly")
    sections: list[SourceSection] = Field(default_factory=list, description="All relevant source sections for this item, each labeled and copied verbatim")
    sourcePassagePage: int | None = Field(None, description="Page number from [PAGE N] markers where the primary section was found")
    sourceDocUrl: str | None = Field(None, description="URL of the source document from AVAILABLE SOURCE DOCUMENTS, if listed")


class SourcePassageExtractions(BaseModel):
    selectedItems: list[SourcePassageItem] = Field(description="The 2-4 most impactful items selected from the candidates, with verbatim source sections")


class PriorityIssueCardText(BaseModel):
    agendaItemTitle: str = Field(description="Title of the agenda item — must match one of the items provided")
    slug: str = Field(description="URL-safe slug derived from the agenda item title")
    headline: str = Field(description="One punchy sentence, max 15 words. What's at stake and what this meeting means.")
    whatYouNeedToDo: str = Field(description="3-5 sentences. First sentence states vote type. Base all claims on the sourcePassage provided.")
    askThisInTheRoom: str = Field(description="One specific question to ask in the meeting, written as a direct quote")
    tryThis: str | None = Field(None, description="Optional suggested talking point")


class BriefingCardTexts(BaseModel):
    executiveHeadline: str
    executiveSubheadline: str
    cards: list[PriorityIssueCardText]


class PriorityIssueDetail(BaseModel):
    whatIsHappening: str = Field(description="~30 words, 2 sentences. Concise context: what is this about and why is it on the agenda now")
    whatDecision: str = Field(description="~25 words, 1-2 sentences. What specific decision is being made or asked of the council member")
    whyItMatters: str = Field(description="50-70 words, 2-3 sentences. Why this matters — include concrete details, dollar amounts, affected areas. Use the full word count.")
    recommendation: str = Field(description="~40 words, 2-3 sentences. A frame for how to think about this decision — what questions to weigh, what trade-offs to understand. Not a task or directive.")
    actionItem: str = Field(description="~28 words, 1 sentence. One specific, concrete, pre-meeting task — name the exact document to read, the person to call, or the specific thing to verify. Not general framing.")
    askThis: str = Field(description="~30 words. A direct-quote question to ask in the meeting")
    tryThis: str | None = Field(None, description="~30 words. Optional suggested statement or talking point")
    whoIsPresenting: str | None = Field(None, description="50-75 words, 1-2 paragraphs. Who is presenting this item — name and title if stated in the agenda or PDF, otherwise the responsible department by type (e.g. 'Public Works' or 'City Manager's office'). Omit entirely if no presenter or responsible department can be identified from the source text. Do not predict vote outcomes or describe political dynamics.")
    supportingContext: str | None = Field(None, description="50-70 words. Background data, statistics, historical context. Use full word count.")


# ============================================================================
# PASS 1: CATEGORIZE AGENDA ITEMS
# ============================================================================

PASS1_CHUNK_SIZE = 15  # Max items per Pass 1 call — keeps structured output within Gemini limits


def _sanitize_for_prompt(text: str) -> str:
    """Remove characters that cause Gemini structured output to truncate mid-JSON."""
    text = text.replace("§", "sec.").replace("\u00a7", "sec.")
    # Strip non-ASCII garbage from PDF extraction (garbled chars cause structured output to bail)
    return "".join(c if ord(c) < 128 else " " for c in text)


def _format_items_text(items: list, start_index: int = 0) -> str:
    """Format agenda items into the text block used in Pass 1 prompts."""
    lines = []
    for i, item in enumerate(items):
        title = _sanitize_for_prompt(item.get("title", ""))
        section = item.get("section", "")
        fiscal = item.get("fiscalAmounts", [])
        hearing = item.get("isPublicHearing", False)
        desc = item.get("description", "")

        parts = [f"[{start_index + i + 1}] {title}"]
        if section:
            parts.append(f"  section: {section}")
        if fiscal:
            amounts = ", ".join(
                str(f) if isinstance(f, (str, int, float))
                else str(f.get("amount", f)) if isinstance(f, dict)
                else str(f)
                for f in fiscal
            )
            parts.append(f"  fiscal: {amounts}")
        if hearing:
            parts.append("  PUBLIC HEARING")
        if desc:
            parts.append(f"  description: {_sanitize_for_prompt(desc)[:200]}")
        lines.append("\n".join(parts))
    return "\n".join(lines)


def _run_pass1_chunk(city, body, date, items_chunk, start_index, constituent_context, pdf_text, gemini) -> list:
    """Run Pass 1 on a single chunk of items. Returns list of CategorizedAgendaItem."""
    items_text = _format_items_text(items_chunk, start_index)
    prompt = build_pass1_prompt(
        city=city, body=body, date=date,
        items_text=items_text,
        constituent_context=constituent_context,
        pdf_text=pdf_text,
    )
    # No thinking for chunks — keep output tight, avoid token bloat on small batches
    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=AgendaCategorization,
        temperature=0.1,
        thinking_budget=0,
        max_tokens=16000,
    )
    if isinstance(result, dict):
        result = AgendaCategorization(**result)
    return result.items


def pass1_categorize(meeting: dict, gemini, constituent: dict | None = None, pdf_text: str = "") -> AgendaCategorization:
    city = meeting.get("cityName", "")
    body = meeting.get("body", "City Council")
    date = meeting.get("date", "")
    items = meeting.get("data", {}).get("agendaItems", [])

    constituent_context = format_top_constituent_issues(constituent, n=7) if constituent else ""

    if len(items) <= PASS1_CHUNK_SIZE:
        # Single call — normal path
        items_text = _format_items_text(items)
        prompt = build_pass1_prompt(
            city=city, body=body, date=date,
            items_text=items_text,
            constituent_context=constituent_context,
            pdf_text=pdf_text,
        )
        use_thinking = len(items) >= 25
        result = gemini.generate_structured_content(
            prompt=prompt,
            response_schema=AgendaCategorization,
            temperature=0.1,
            thinking_budget=None if use_thinking else 0,
            max_tokens=32000,
        )
        if isinstance(result, dict):
            return AgendaCategorization(**result)
        return result

    # Chunked path for large agendas — process in batches then merge
    chunks = [items[i:i + PASS1_CHUNK_SIZE] for i in range(0, len(items), PASS1_CHUNK_SIZE)]
    print(f"  Large agenda ({len(items)} items) — splitting into {len(chunks)} chunks of {PASS1_CHUNK_SIZE}")

    all_categorized = []
    for chunk_idx, chunk in enumerate(chunks):
        start = chunk_idx * PASS1_CHUNK_SIZE
        print(f"  Pass 1 chunk {chunk_idx + 1}/{len(chunks)}: items {start + 1}–{start + len(chunk)}")
        # Don't pass pdf_text to chunks — the full PDF contains all agenda items and causes
        # the LLM to extract items beyond the 15-item slice it was given.
        chunk_items = _run_pass1_chunk(city, body, date, chunk, start, constituent_context, "", gemini)
        all_categorized.extend(chunk_items)

    # Merge: generate a real one-sentence agendaSummary from the full categorized item list.
    # The generic count template is not useful in Pass 2 context or the user-visible fullAgendaSummary.
    agenda_summary = _generate_agenda_summary(city, body, date, all_categorized, gemini)

    return AgendaCategorization(items=all_categorized, agendaSummary=agenda_summary)


class _AgendaSummaryResult(BaseModel):
    agendaSummary: str


def _generate_agenda_summary(
    city: str,
    body: str,
    date: str,
    items: list[CategorizedAgendaItem],
    gemini,
) -> str:
    """Generate a real one-sentence agenda summary after chunked Pass 1 completes."""
    items_list = "\n".join(
        f"- [{item.category}] {item.title}" + (" (priority)" if item.isPriority else "")
        for item in items
    )
    prompt = (
        f"You summarized a {city} {body} agenda for {date} in chunks. "
        f"Here is the full categorized item list:\n{items_list}\n\n"
        "Write a single sentence summarizing the full agenda — what the meeting covers and what kinds of action are required. "
        "Be specific: name the main topics or themes. Do not use the word 'briefing'."
    )
    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=_AgendaSummaryResult,
        temperature=0.1,
        thinking_budget=0,
        max_tokens=200,
    )
    if isinstance(result, dict):
        return result.get("agendaSummary", "")
    return result.agendaSummary


# ============================================================================
# PASS 2: GENERATE CARD CONTENT FOR PRIORITY ISSUES
# ============================================================================

def pass2_generate_cards(meeting: dict, categorized: AgendaCategorization, gemini, constituent: dict | None = None, pdf_text: str = "", available_docs: list[dict] | None = None) -> BriefingCards:
    city = meeting.get("cityName", "")
    body = meeting.get("body", "City Council")
    date = meeting.get("date", "")

    priority_items = sorted(
        [item for item in categorized.items if item.isPriority],
        key=lambda x: x.priorityScore,
        reverse=True,
    )

    if not priority_items:
        priority_items = [
            item for item in categorized.items
            if item.category not in ("procedural", "consent")
        ][:4]

    priority_candidates = priority_items[:8]

    _VOTE_FLAG = {
        "vote_required": "⚡ VOTE REQUIRED",
        "direction_setting": "💬 DIRECTION SETTING (no vote, but your words shape the outcome)",
        "public_hearing": "🎤 PUBLIC HEARING (vote may follow)",
        "informational": "ℹ️ INFORMATIONAL (no vote)",
    }

    items_text = []
    for i, item in enumerate(priority_candidates):
        vote_flag = _VOTE_FLAG.get(item.category, "")
        items_text.append(
            f"[{i+1}] {item.title} (score: {item.priorityScore}/10)\n"
            f"  Category: {item.category}{('  ' + vote_flag) if vote_flag else ''}"
        )

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        day_name = dt.strftime("%A")
    except ValueError:
        day_name = "the upcoming"

    available_docs_str = _format_available_docs(available_docs, meeting.get("sourceUrl"))

    # Pass 2a: select items and extract verbatim source passages (temp 0.1 — transcription task)
    prompt_2a = build_pass2a_prompt(
        city=city,
        body=body,
        date=date,
        items_text="\n".join(items_text),
        pdf_text=pdf_text,
        available_docs=available_docs_str,
    )
    result_2a = gemini.generate_structured_content(
        prompt=prompt_2a,
        response_schema=SourcePassageExtractions,
        temperature=0.1,
        thinking_budget=0,
    )
    if isinstance(result_2a, dict):
        result_2a = SourcePassageExtractions(**result_2a)

    # Infer is_prior_minutes from label when the LLM left it None/False but the label signals minutes.
    # This catches cases where the model correctly names the section "April 7 Meeting Minutes" but
    # doesn't populate the boolean field.
    _MINUTES_LABEL_SIGNALS = re.compile(
        r"\b(minutes|meeting minutes|council minutes|workshop minutes|special meeting minutes)\b",
        re.IGNORECASE,
    )
    for sel_item in result_2a.selectedItems:
        for sec in sel_item.sections:
            if not sec.is_prior_minutes and _MINUTES_LABEL_SIGNALS.search(sec.label):
                sec.is_prior_minutes = True

    # Pass 2b: write card text using the extracted passages as grounding (temp 0.3 — writing task)
    def _format_sections_for_prompt(item: SourcePassageItem) -> str:
        if item.sections:
            parts = []
            for sec in item.sections:
                tag = " [is_prior_minutes=true]" if sec.is_prior_minutes else ""
                parts.append(f"[{sec.label}{tag}]\n{sec.text}")
            return "\n\n".join(parts)
        return "(no source passages found)"

    passages_text = "\n\n".join(
        f"[{i+1}] {item.agendaItemTitle}\n"
        f"SOURCE PASSAGES:\n{_format_sections_for_prompt(item)}"
        for i, item in enumerate(result_2a.selectedItems)
    )
    constituent_context = format_constituent_context(constituent, n=5) if constituent else ""
    prompt_2b = build_pass2b_prompt(
        city=city,
        body=body,
        date=date,
        day_name=day_name,
        passages_text=passages_text,
        agenda_summary=categorized.agendaSummary,
        total_items=len(categorized.items),
        constituent_context=constituent_context,
    )
    result_2b = gemini.generate_structured_content(
        prompt=prompt_2b,
        response_schema=BriefingCardTexts,
        temperature=0.3,
        thinking_budget=0,
    )
    if isinstance(result_2b, dict):
        result_2b = BriefingCardTexts(**result_2b)

    # Merge: passages from 2a + card text from 2b → BriefingCards
    passage_by_title = {item.agendaItemTitle: item for item in result_2a.selectedItems}
    issues = []
    for card_text in result_2b.cards:
        passage = passage_by_title.get(card_text.agendaItemTitle)
        sections = passage.sections if passage else []
        # sourcePassage = text of first section (backward compat for downstream consumers)
        primary_passage = sections[0].text if sections else None
        issues.append(PriorityIssueCard(
            agendaItemTitle=card_text.agendaItemTitle,
            slug=card_text.slug,
            sourcePassage=primary_passage,
            sourceSections=sections,
            sourcePassagePage=passage.sourcePassagePage if passage else None,
            sourceDocUrl=passage.sourceDocUrl if passage else None,
            headline=card_text.headline,
            whatYouNeedToDo=card_text.whatYouNeedToDo,
            askThisInTheRoom=card_text.askThisInTheRoom,
            tryThis=card_text.tryThis,
        ))

    return BriefingCards(
        executiveHeadline=result_2b.executiveHeadline,
        executiveSubheadline=result_2b.executiveSubheadline,
        priorityIssues=issues,
    )


# ============================================================================
# PASS 3: GENERATE DETAIL CONTENT FOR EACH PRIORITY ISSUE
# ============================================================================

def _format_available_docs(docs: list[dict] | None, meeting_source_url: str | None) -> str:
    lines = []
    if meeting_source_url:
        lines.append(f"  - Meeting page: {meeting_source_url}")
    for doc in (docs or []):
        name = doc.get("name", "Document")
        url = doc.get("url", "")
        if url:
            lines.append(f"  - {name}: {url}")
    if not lines:
        return ""
    return "\nAVAILABLE SOURCE DOCUMENTS (use these URLs to populate supportingDocuments):\n" + "\n".join(lines)


_PDF_TEXT_CHAR_LIMIT = 100_000  # ~75K tokens — well within Gemini Flash's 1M context


def _load_pdf_text(meeting: dict, storage) -> str:
    """
    Load and return the full text of the agenda PDF for this meeting.
    Returns "" if no PDF is available or extraction fails.
    Capped at _PDF_TEXT_CHAR_LIMIT characters to stay within context budget.
    """
    if not storage:
        return ""
    # agendaFiles is at the top level of the meeting dict (set by normalized_to_meeting_dict)
    agenda_files = meeting.get("agendaFiles") or []
    pdf_storage_key = next(
        (f.get("url") for f in agenda_files if f.get("type") == "storage_pdf"),
        None,
    )
    if not pdf_storage_key:
        return ""
    try:
        pdf_bytes = storage.read_bytes(pdf_storage_key)
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(f"[PAGE {i+1}]\n{doc[i].get_text()}" for i in range(min(len(doc), 60)))
        if len(text) > _PDF_TEXT_CHAR_LIMIT:
            text = text[:_PDF_TEXT_CHAR_LIMIT]
        return text.strip()
    except Exception:
        return ""



def _window_pdf_around_page(pdf_text: str, page: int, radius: int = 3) -> str:
    """Return a slice of pdf_text covering pages within `radius` of `page`.

    Uses [PAGE N] markers (added by _load_pdf_text and extract_pdf_text) to locate
    page boundaries. Falls back to the full pdf_text if markers aren't found.

    The lower bound is clamped to `page` itself — we never look before the source page.
    This prevents early-page items (sourcePassagePage=1) from pulling in prior-meeting
    minutes or roll calls that appear at the top of the PDF.
    """
    if not pdf_text or not page:
        return pdf_text
    page_matches = list(re.finditer(r'\[PAGE (\d+)\]', pdf_text))
    if not page_matches:
        return pdf_text
    pages = [(int(m.group(1)), m.start()) for m in page_matches]
    lo, hi = page, page + radius  # never look before the source page
    in_window = [(p, pos) for p, pos in pages if lo <= p <= hi]
    if not in_window:
        return pdf_text
    start = in_window[0][1]
    after_window = [(p, pos) for p, pos in pages if p > hi]
    end = after_window[0][1] if after_window else len(pdf_text)
    return pdf_text[start:end]


def pass3_generate_detail(
    meeting: dict,
    card: PriorityIssueCard,
    categorized_item: CategorizedAgendaItem,
    all_items: list[CategorizedAgendaItem],
    gemini,
    constituent: dict | None = None,
    available_docs: list[dict] | None = None,
    storage=None,
    pdf_text: str = "",
) -> PriorityIssueDetail:
    city = meeting.get("cityName", "")
    body = meeting.get("body", "City Council")
    date = meeting.get("date", "")
    state = meeting.get("state", "")

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        day_name = dt.strftime("%A")
    except ValueError:
        day_name = "the upcoming"

    # sourcePassage from Pass 2 is the verbatim ground truth for Pass 3.
    # Caller is responsible for gating on sourcePassage before calling this function.
    # Format source sections for the prompt — these are the sole ground truth for Pass 3.
    # No windowed PDF is passed; all grounding comes from the curated sections extracted in Pass 2a.
    if card.sourceSections:
        source_sections_str = "\n\n".join(
            f"[{sec.label}{' [is_prior_minutes=true]' if sec.is_prior_minutes else ''}]\n{sec.text}"
            for sec in card.sourceSections
        )
    elif card.sourcePassage:
        # Backward compat: single passage with no label
        source_sections_str = f"[Source Text]\n{card.sourcePassage}"
    else:
        source_sections_str = "(no source sections available)"

    constituent_context = format_constituent_context(constituent, n=5) if constituent else ""
    other_items = ", ".join(
        item.title for item in all_items if item.title != card.agendaItemTitle
    )[:500]
    available_docs_str = _format_available_docs(available_docs, meeting.get("sourceUrl"))
    prompt = build_pass3_prompt(
        city=city,
        state=state,
        body=body,
        date=date,
        day_name=day_name,
        agenda_item_title=card.agendaItemTitle,
        category=categorized_item.category,
        headline=card.headline,
        source_sections=source_sections_str,
        other_items=other_items,
        constituent_context=constituent_context,
        available_docs=available_docs_str,
    )

    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=PriorityIssueDetail,
        temperature=0.3,
    )

    if isinstance(result, dict):
        return PriorityIssueDetail(**result)
    return result


# ============================================================================
# PROVENANCE VALIDATOR
# ============================================================================

# Fields where the prompt requires source-grounded claims only.
_INFERRED_PATTERN = re.compile(
    r'\b(?:typically|generally|usually|historically|often|commonly|tends to|in most cases)\b',
    re.IGNORECASE,
)
# Matches sequences of two or more Title Case words — likely proper names or named roles.
# Used to catch claims about specific people or departments not found in the source PDF.
_PROPER_NAME_PATTERN = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b')
_GROUNDED_FIELDS = ("whoIsPresenting", "supportingContext")


def check_provenance(details: list, cards: "BriefingCards") -> list[str]:
    """
    Check whoIsPresenting and supportingContext in Pass 3 output for two classes of problems:

    1. Hedging language without an 'Inferred:' prefix — catches cautious fabrication.
    2. Named persons or roles (Title Case multi-word phrases) that do not appear verbatim
       in any source section — catches confident fabrication.

    Args:
        details: List of PriorityIssueDetail objects (may contain None for skipped items).
        cards: BriefingCards with sourceSections for each priority issue — used as
               the ground truth corpus for named entity verification.
    """
    # Build source corpus from all extracted source sections
    source_parts = []
    for card in cards.priorityIssues:
        if card.sourceSections:
            source_parts.extend(sec.text for sec in card.sourceSections if sec.text)
        elif card.sourcePassage:
            source_parts.append(card.sourcePassage)
    source_corpus = " ".join(source_parts)

    warnings = []
    for detail in details:
        if detail is None:
            continue
        for field in _GROUNDED_FIELDS:
            value = getattr(detail, field, None) or ""
            if not value:
                continue
            # Check 1: hedging language without Inferred: prefix
            for sentence in re.split(r'(?<=[.!?])\s+', value):
                if _INFERRED_PATTERN.search(sentence) and not sentence.strip().startswith("Inferred:"):
                    warnings.append(
                        f"{field} may contain untagged inference: \"{sentence.strip()[:120]}\""
                    )
            # Check 2: named persons/roles not found in the source sections
            if source_corpus:
                for name in _PROPER_NAME_PATTERN.findall(value):
                    if name not in source_corpus:
                        warnings.append(
                            f"{field} references '{name}' which does not appear in the source sections"
                        )
    return warnings


# ============================================================================
# FISCAL AMOUNT CROSS-CHECK
# ============================================================================

_DOLLAR_PATTERN = re.compile(
    r'\$[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|M|B|K))?'
    r'|\b\d[\d,]*(?:\.\d+)?\s*(?:million|billion|thousand)\b',
    re.IGNORECASE,
)


def _normalize_amount(s: str) -> str:
    """Strip formatting for loose comparison."""
    return re.sub(r'[\s,$]', '', s).lower()


def check_fiscal_amounts(briefing: dict, cards: "BriefingCards") -> list[str]:
    """
    Extract all dollar amounts from generated briefing text and check each
    appears in the verbatim sourcePassage. Returns list of warning strings
    for amounts that could not be verified.
    """
    # Build source corpus from all verbatim source sections (ground truth)
    source_parts = []
    for card in cards.priorityIssues:
        if card.sourceSections:
            source_parts.extend(sec.text for sec in card.sourceSections if sec.text)
        elif card.sourcePassage:
            source_parts.append(card.sourcePassage)
    source_text = " ".join(source_parts)
    source_amounts = {_normalize_amount(m) for m in _DOLLAR_PATTERN.findall(source_text)}

    # Extract amounts from generated briefing text (cards + details)
    generated_text = json.dumps(briefing.get("priorityIssues", []))
    generated_amounts = _DOLLAR_PATTERN.findall(generated_text)

    warnings = []
    for amount in generated_amounts:
        norm = _normalize_amount(amount)
        if norm and not any(norm in s or s in norm for s in source_amounts):
            warnings.append(f"Unverified amount in briefing: '{amount}' not found in normalized agenda source")

    return warnings


# ============================================================================
# ASSEMBLE FINAL BRIEFING JSON
# ============================================================================

def _make_meeting_title(city_name: str, body: str) -> str:
    body_clean = re.sub(r"\s+meeting$", "", body, flags=re.IGNORECASE).strip()
    return f"{city_name} {body_clean} Meeting Briefing"


def assemble_briefing(
    meeting: dict,
    categorized: AgendaCategorization,
    cards: BriefingCards,
    details: list[PriorityIssueDetail],
    constituent: dict | None = None,
) -> dict:
    date = meeting.get("date", "")

    full_agenda = []
    for i, item in enumerate(categorized.items):
        agenda_item = {
            "number": str(i + 1),
            "title": item.title,
            "description": item.description,
            "category": item.category,
        }
        for j, card in enumerate(cards.priorityIssues):
            if card.agendaItemTitle.lower().strip() == item.title.lower().strip():
                agenda_item["isPriority"] = True
                agenda_item["priorityNumber"] = j + 1
                break
        full_agenda.append(agenda_item)

    priority_issues = []
    for i, card in enumerate(cards.priorityIssues):
        detail = details[i] if i < len(details) else None

        issue = {
            "number": i + 1,
            "slug": card.slug,
            "agendaItemTitle": card.agendaItemTitle,
            "category": next(
                (item.category for item in categorized.items
                 if item.title.lower().strip() == card.agendaItemTitle.lower().strip()),
                "other"
            ),
            "sourcePassage": card.sourcePassage,
            "sourceSections": [
                {"label": s.label, "text": s.text, "page": s.page, "is_prior_minutes": s.is_prior_minutes or False}
                for s in card.sourceSections
            ],
            "sourcePassagePage": card.sourcePassagePage,
            "sourceDocUrl": card.sourceDocUrl,
            "card": {
                "headline": card.headline,
                "whatYouNeedToDo": card.whatYouNeedToDo,
                "askThisInTheRoom": card.askThisInTheRoom,
                "tryThis": card.tryThis,
                "actionButtons": [],
            },
        }

        if detail:
            # Exclude storage_pdf entries (S3 keys, not public URLs)
            supporting_docs = [
                f for f in meeting.get("agendaFiles", [])
                if f.get("type") != "storage_pdf" and isinstance(f.get("url"), str) and f["url"].startswith("http")
            ]
            if meeting.get("sourceUrl"):
                supporting_docs.append({"name": "Meeting agenda page", "url": meeting["sourceUrl"]})

            issue["detail"] = {
                "whatIsHappening": detail.whatIsHappening,
                "whatDecision": detail.whatDecision,
                "whyItMatters": detail.whyItMatters,
                "recommendation": detail.recommendation,
                "actionItem": detail.actionItem,
                "askThis": detail.askThis,
                "tryThis": detail.tryThis,
                "whoIsPresenting": detail.whoIsPresenting,
                "supportingContext": detail.supportingContext,
                "supportingDocuments": supporting_docs,
            }

        priority_issues.append(issue)

    detail_text = " ".join(
        " ".join(filter(None, [
            issue.get("detail", {}).get("whatIsHappening", ""),
            issue.get("detail", {}).get("whatDecision", ""),
            issue.get("detail", {}).get("whyItMatters", ""),
            issue.get("detail", {}).get("recommendation", ""),
            issue.get("detail", {}).get("actionItem", ""),
            issue.get("card", {}).get("headline", ""),
            issue.get("card", {}).get("whatYouNeedToDo", ""),
        ]))
        for issue in priority_issues
    )
    word_count = len(detail_text.split())
    read_minutes = max(3, round(word_count / 250))

    return {
        "version": "1.0",
        "generatedAt": datetime.now().isoformat(),
        "generationModel": "gemini-2.5-flash",

        "meeting": {
            "citySlug": meeting.get("citySlug", ""),
            "cityName": meeting.get("cityName", ""),
            "state": meeting.get("state", ""),
            "body": meeting.get("body", ""),
            "date": date,
            "time": meeting.get("time"),
            "title": _make_meeting_title(meeting.get("cityName", ""), meeting.get("body", "")),
            "readTime": f"{read_minutes} Minute Read",
            "sourceUrl": meeting.get("sourceUrl"),
            "sourceType": meeting.get("sourceType", ""),
        },

        "executiveSummary": {
            "headline": cards.executiveHeadline,
            "subheadline": cards.executiveSubheadline,
            "priorityItemCount": len(priority_issues),
            "totalAgendaItems": len(full_agenda),
        },

        "priorityIssues": priority_issues,

        "fullAgenda": full_agenda,
        "fullAgendaSummary": categorized.agendaSummary,

        "constituentData": {
            "available": constituent is not None,
            "voterCount": constituent.get("voter_count_with_scores") if constituent else None,
            "topIssues": [
                {"name": i["name"], "score": round(i["score"]), "tier": i["tier_label"]}
                for i in sorted(
                    constituent.get("issues", []),
                    key=lambda x: x.get("score", 0),
                    reverse=True,
                )[:5]
            ] if constituent else [],
            "ideology": constituent.get("context_scores") if constituent else None,
        },

        "footer": {
            "preparedBy": "GoodParty.org",
            "contactNote": "Questions about this briefing? Reply to your briefing email.",
        },
    }


# ============================================================================
# MAIN PIPELINE
# ============================================================================

def generate_briefing_for_meeting(
    normalized_key: str,
    storage,
    cfg: AgentConfig,
    dry_run: bool = False,
) -> dict:
    """Run the full briefing generation pipeline for one normalized meeting JSON."""
    from shared.llm_gemini import GeminiClient, GeminiModelType

    normalized = storage.read_json(normalized_key)
    meeting = normalized_to_meeting_dict(normalized)
    city = meeting.get("cityName", "")
    city_slug = meeting.get("citySlug", "")
    date = meeting.get("date", "")
    items = meeting.get("data", {}).get("agendaItems", [])

    substantive_items = [i for i in items if i.get("section") not in ("procedural",)]
    print(f"\n  Generating briefing for {city} — {date} ({len(items)} agenda items, {len(substantive_items)} substantive)")

    if len(items) < 3:
        print(f"  SKIP: Too few agenda items ({len(items)})")
        return {"status": "skipped", "reason": "too_few_items"}

    if len(substantive_items) < 2:
        print(f"  SKIP: Too few substantive agenda items ({len(substantive_items)} non-procedural) — agenda likely not posted yet")
        return {"status": "skipped", "reason": "too_few_substantive_items"}

    # Check for manual pipeline exclusion marker
    if city_slug:
        exclusion_key = f"{cfg.sources_prefix}/{city_slug}/pipeline_exclusion.json"
        if storage.exists(exclusion_key):
            excl = storage.read_json(exclusion_key)
            print(f"  SKIP: {city_slug} is excluded — {excl.get('reason')}: {excl.get('detail', '')}")
            return {"status": "skipped", "reason": "pipeline_excluded", "detail": excl.get("reason")}

    constituent = load_constituent_data(city_slug, storage, cfg.sources_prefix) if city_slug else None
    if constituent:
        top = format_top_constituent_issues(constituent)
        print(f"  Haystaq data: {constituent.get('voter_count_with_scores', 0):,} voters — top: {top}")
    else:
        print(f"  No Haystaq constituent data for {city_slug} — proceeding without constituent framing")

    gemini = GeminiClient(default_model=GeminiModelType.FLASH)

    # Load the agenda PDF text once — passed to all three passes as primary source
    pdf_text = _load_pdf_text(meeting, storage)
    if pdf_text:
        print(f"  PDF context: {len(pdf_text.split()):,} words loaded for all passes")
    else:
        print("  PDF context: not available (will use normalized fields only)")

    # Pass 1: Categorize
    print(f"  Pass 1: Categorizing {len(items)} agenda items...")
    t0 = time.time()
    categorized = pass1_categorize(meeting, gemini, constituent=constituent, pdf_text=pdf_text)
    t1 = time.time()
    priority_count = sum(1 for item in categorized.items if item.isPriority)
    print(f"  Pass 1 done: {len(categorized.items)} items, {priority_count} priorities ({t1-t0:.1f}s)")

    # Pass 2: Generate cards
    print("  Pass 2: Generating card content...")
    t0 = time.time()
    cards = pass2_generate_cards(meeting, categorized, gemini, constituent=constituent, pdf_text=pdf_text, available_docs=meeting.get("agendaFiles"))
    t1 = time.time()
    print(f"  Pass 2 done: {len(cards.priorityIssues)} priority cards ({t1-t0:.1f}s)")
    for card in cards.priorityIssues:
        if not card.sourcePassage:
            print(f"  ⚠ No sourcePassage for priority item: '{card.agendaItemTitle[:60]}' — detail will be ungrounded")

    # Pass 3: Generate details for each priority
    # Gate: skip items with no sourcePassage — a card without a detail page is more
    # honest than a detail page built on nothing.
    details = []
    for i, card in enumerate(cards.priorityIssues):
        if not card.sourcePassage or len(card.sourcePassage) < 80:
            print(f"  Pass 3.{i+1}: SKIP '{card.agendaItemTitle[:60]}' — sourcePassage missing or too short")
            details.append(None)
            continue

        print(f"  Pass 3.{i+1}: Deep-dive '{card.agendaItemTitle[:50]}...'")
        t0 = time.time()

        cat_item = next(
            (item for item in categorized.items
             if item.title.lower().strip() == card.agendaItemTitle.lower().strip()),
            categorized.items[0] if categorized.items else None,
        )

        if cat_item:
            detail = pass3_generate_detail(
                meeting, card, cat_item, categorized.items, gemini,
                constituent=constituent,
                available_docs=meeting.get("agendaFiles"),
                storage=storage,
                pdf_text=pdf_text,
            )
            details.append(detail)
            t1 = time.time()
            print(f"  Pass 3.{i+1} done ({t1-t0:.1f}s)")
        else:
            details.append(None)

    # Provenance check — validate generated text against source sections
    provenance_warnings = check_provenance(details, cards)
    if provenance_warnings:
        for w in provenance_warnings:
            print(f"  ⚠ Provenance: {w}")

    # Assemble
    print("  Assembling briefing...")
    briefing = assemble_briefing(meeting, categorized, cards, details, constituent=constituent)
    if provenance_warnings:
        briefing["provenanceWarnings"] = provenance_warnings

    # Fiscal cross-check
    fiscal_warnings = check_fiscal_amounts(briefing, cards)
    if fiscal_warnings:
        for w in fiscal_warnings:
            print(f"  ⚠ {w}")
        briefing["fiscalWarnings"] = fiscal_warnings

    # Cost
    stats = gemini.get_usage_stats()
    cost = stats.get("total_cost", 0)
    calls = stats.get("api_call_count", 0)
    briefing["generationCostUsd"] = round(cost, 6)
    print(f"  Cost: ${cost:.4f} ({calls} API calls)")

    # Save — dry-run goes to /tmp, real run goes to storage
    safe_name = f"{city_slug}_{date}_briefing.json"
    if dry_run:
        local_path = f"/tmp/{safe_name}"
        import json as _json
        with Path(local_path).open("w") as f:
            _json.dump(briefing, f, indent=2)
        print(f"  DRY RUN — saved locally: {local_path}")
        return {
            "status": "dry_run",
            "output": local_path,
            "cost": cost,
            "haystaq_available": constituent is not None,
            "fiscal_warnings": fiscal_warnings,
        }

    output_key = f"{cfg.output_prefix}/briefings/{safe_name}"
    storage.write_json(output_key, briefing)
    print(f"  Saved: {output_key}")

    return {
        "status": "ok",
        "output": output_key,
        "cost": cost,
        "haystaq_available": constituent is not None,
        "fiscal_warnings": fiscal_warnings,
    }


