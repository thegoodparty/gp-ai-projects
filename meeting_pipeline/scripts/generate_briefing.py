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

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.prompts.briefing import (
    EDITORIAL_RULES,
    build_pass1_prompt,
    build_pass2_prompt,
    build_pass3_prompt,
)


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

    # Build supporting docs from agenda_files (exclude local PDFs)
    agenda_files = [
        {"name": f.get("name", ""), "url": f.get("url", "")}
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

def load_constituent_data(city_slug: str, storage, sources_prefix: str) -> Optional[dict]:
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


def format_constituent_context(constituent: dict) -> str:
    """Format constituent data into a text block for LLM prompts using tiered prose."""
    issues = constituent.get("issues", [])
    if not issues:
        return ""

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

class CategorizedAgendaItem(BaseModel):
    originalTitle: str = Field(description="Original title from the agenda")
    title: str = Field(description="Clean, readable title (fix ALL CAPS, remove numbering prefixes)")
    description: str = Field(description="1-2 sentence plain-language description of what this item is about")
    category: str = Field(description="One of: procedural, consent, informational, vote_required, direction_setting, public_hearing")
    isPriority: bool = Field(False, description="True if this item is a genuine policy decision, significant spending, or high public interest")
    priorityScore: int = Field(0, description="0 if not priority. 1-10 if priority: 10=highest impact (major policy/budget), 1=lowest (routine vote). Only score items where isPriority=true.")
    priorityReason: Optional[str] = Field(None, description="If isPriority, brief reason why (e.g. 'Large contract approval', 'Zoning change with public hearing')")


class AgendaCategorization(BaseModel):
    items: list[CategorizedAgendaItem]
    agendaSummary: str = Field(description="One-sentence summary of the full agenda (e.g. '12 items including votes, direction-setting discussions, and procedural business.')")


class PriorityIssueCard(BaseModel):
    agendaItemTitle: str = Field(description="Title of the underlying agenda item")
    slug: str = Field(description="URL-safe slug (e.g. 'public-safety-camera-expansion')")
    headline: str = Field(description="One punchy sentence, max 15 words. What's at stake for constituents and what this meeting means — in a single breath. No scores, percentages, or numeric rankings.")
    whatYouNeedToDo: str = Field(description="Actionable paragraph: what the council member should do about this item, what's being decided, what to watch for")
    askThisInTheRoom: str = Field(description="A specific question the council member could ask during the meeting")
    tryThis: Optional[str] = Field(None, description="Optional: a suggested statement or talking point the council member could use")


class BriefingCards(BaseModel):
    executiveHeadline: str = Field(description="Executive summary headline (e.g. 'Monday's meeting has 3 items that directly connect to your platform.')")
    executiveSubheadline: str = Field(description="Follow-up line (e.g. 'Here's what requires your attention and what to do about each one:')")
    priorityIssues: list[PriorityIssueCard] = Field(description="2-4 priority issues, ordered by importance")


class PriorityIssueDetail(BaseModel):
    whatIsHappening: str = Field(description="~30 words, 2 sentences. Concise context: what is this about and why is it on the agenda now")
    whatDecision: str = Field(description="~25 words, 1-2 sentences. What specific decision is being made or asked of the council member")
    whyItMatters: str = Field(description="50-70 words, 2-3 sentences. Why this matters — include concrete details, dollar amounts, affected areas. Use the full word count.")
    recommendation: str = Field(description="~40 words, 2-3 sentences. Clear recommendation with reasoning: what to do AND why")
    actionItem: str = Field(description="~28 words, 1 sentence. One specific pre-meeting action to take")
    askThis: str = Field(description="~30 words. A direct-quote question to ask in the meeting")
    tryThis: Optional[str] = Field(None, description="~30 words. Optional suggested statement or talking point")
    whoIsPresenting: str = Field(description="50-75 words, 1-2 paragraphs. Who is presenting (department/role if name unknown), political dynamics, expected council reception. Always required.")
    supportingContext: Optional[str] = Field(None, description="50-70 words. Background data, statistics, historical context. Use full word count.")


# ============================================================================
# PASS 1: CATEGORIZE AGENDA ITEMS
# ============================================================================

def pass1_categorize(meeting: dict, gemini, constituent: Optional[dict] = None) -> AgendaCategorization:
    city = meeting.get("cityName", "")
    body = meeting.get("body", "City Council")
    date = meeting.get("date", "")
    items = meeting.get("data", {}).get("agendaItems", [])

    items_text = []
    for i, item in enumerate(items):
        title = item.get("title", "")
        section = item.get("section", "")
        fiscal = item.get("fiscalAmounts", [])
        hearing = item.get("isPublicHearing", False)
        desc = item.get("description", "")

        parts = [f"[{i+1}] {title}"]
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
            parts.append(f"  description: {desc[:200]}")
        items_text.append("\n".join(parts))

    constituent_context = format_top_constituent_issues(constituent, n=7) if constituent else ""
    prompt = build_pass1_prompt(
        city=city,
        body=body,
        date=date,
        items_text="\n".join(items_text),
        constituent_context=constituent_context,
    )

    # Disable thinking for small agendas — categorizing a short list is straightforward
    # pattern-matching that doesn't benefit from reasoning. For large agendas, thinking
    # produces better holistic prioritization and avoids structured output truncation.
    use_thinking = len(items) >= 25
    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=AgendaCategorization,
        temperature=0.1,
        thinking_budget=None if use_thinking else 0,
    )

    if isinstance(result, dict):
        return AgendaCategorization(**result)
    return result


# ============================================================================
# PASS 2: GENERATE CARD CONTENT FOR PRIORITY ISSUES
# ============================================================================

def pass2_generate_cards(meeting: dict, categorized: AgendaCategorization, gemini, constituent: Optional[dict] = None) -> BriefingCards:
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
            f"  Category: {item.category}{('  ' + vote_flag) if vote_flag else ''}\n"
            f"  Description: {item.description}\n"
            f"  Priority reason: {item.priorityReason or 'N/A'}"
        )

    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        day_name = dt.strftime("%A")
    except ValueError:
        day_name = "the upcoming"

    constituent_context = format_constituent_context(constituent) if constituent else ""
    prompt = build_pass2_prompt(
        city=city,
        body=body,
        date=date,
        day_name=day_name,
        items_text="\n".join(items_text),
        agenda_summary=categorized.agendaSummary,
        total_items=len(categorized.items),
        constituent_context=constituent_context,
    )

    result = gemini.generate_structured_content(
        prompt=prompt,
        response_schema=BriefingCards,
        temperature=0.3,
        thinking_budget=0,
    )

    if isinstance(result, dict):
        return BriefingCards(**result)
    return result


# ============================================================================
# PASS 3: GENERATE DETAIL CONTENT FOR EACH PRIORITY ISSUE
# ============================================================================

def _format_available_docs(docs: Optional[list[dict]], meeting_source_url: Optional[str]) -> str:
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


def _extract_item_passage(pdf_bytes: bytes, item_title: str, context_chars: int = 2000) -> str:
    """Extract a passage from the raw PDF near the agenda item title."""
    try:
        import fitz
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        full_text = "\n".join(doc[i].get_text() for i in range(min(len(doc), 60)))
        title_lower = item_title.lower()
        idx = full_text.lower().find(title_lower[:40])  # search first 40 chars of title
        if idx == -1:
            # fallback: try first 20 chars
            idx = full_text.lower().find(title_lower[:20])
        if idx == -1:
            return ""
        start = max(0, idx - 200)
        end = min(len(full_text), idx + context_chars)
        return full_text[start:end].strip()
    except Exception:
        return ""


def pass3_generate_detail(
    meeting: dict,
    card: PriorityIssueCard,
    categorized_item: CategorizedAgendaItem,
    all_items: list[CategorizedAgendaItem],
    gemini,
    constituent: Optional[dict] = None,
    available_docs: Optional[list[dict]] = None,
    storage=None,
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

    raw_items = meeting.get("data", {}).get("agendaItems", [])
    raw_item = next(
        (it for it in raw_items
         if (it.get("title") or "").lower().strip() == card.agendaItemTitle.lower().strip()),
        None,
    )
    source_text_parts = []
    if raw_item:
        if raw_item.get("description"):
            source_text_parts.append(f"Description: {raw_item['description']}")
        if raw_item.get("staffRecommendation"):
            source_text_parts.append(f"Staff recommendation: {raw_item['staffRecommendation']}")
        if raw_item.get("presenter"):
            source_text_parts.append(f"Presenter: {raw_item['presenter']}")
        if raw_item.get("fiscalAmounts"):
            source_text_parts.append(f"Fiscal amounts: {raw_item['fiscalAmounts']}")

    # Inject raw PDF passage as primary source (avoids grounding against lossy LLM intermediate)
    if storage:
        agenda_files = meeting.get("sources", {}).get("agendaFiles") or []
        pdf_storage_key = next(
            (f.get("url") for f in agenda_files if f.get("type") == "storage_pdf"),
            None,
        )
        if pdf_storage_key:
            try:
                pdf_bytes = storage.read_bytes(pdf_storage_key)
                passage = _extract_item_passage(pdf_bytes, card.agendaItemTitle)
                if passage:
                    source_text_parts.append(f"\nRAW AGENDA SOURCE (primary):\n{passage}")
            except Exception:
                pass  # fall back to normalized fields only

    source_text = "\n".join(source_text_parts) if source_text_parts else "No additional source text available for this item."

    constituent_context = format_constituent_context(constituent) if constituent else ""
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
        description=categorized_item.description,
        priority_reason=categorized_item.priorityReason or "",
        headline=card.headline,
        source_text=source_text,
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
# Any specific name, dollar amount, or statistic should be prefixed "Inferred:"
# if it doesn't appear in the source text.
_INFERRED_PATTERN = re.compile(
    r'\b(?:typically|generally|usually|historically|often|commonly|tends to|in most cases)\b',
    re.IGNORECASE,
)
_GROUNDED_FIELDS = ("whoIsPresenting", "supportingContext")


def check_provenance(details: list, source_texts: dict[str, str]) -> list[str]:
    """
    Check that restricted fields in Pass 3 output don't contain likely-inferred
    claims without the 'Inferred:' prefix. Returns list of warning strings.
    """
    warnings = []
    for detail in details:
        title = getattr(detail, "agendaItemTitle", "") if hasattr(detail, "agendaItemTitle") else ""
        source = source_texts.get(title, "")
        for field in _GROUNDED_FIELDS:
            value = getattr(detail, field, None) or ""
            if not value:
                continue
            # Flag sentences with inference language but no Inferred: prefix
            for sentence in re.split(r'(?<=[.!?])\s+', value):
                if _INFERRED_PATTERN.search(sentence) and not sentence.strip().startswith("Inferred:"):
                    warnings.append(
                        f"{field} may contain untagged inference: \"{sentence.strip()[:120]}\""
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


def check_fiscal_amounts(briefing: dict, meeting: dict) -> list[str]:
    """
    Extract all dollar amounts from generated briefing text and check each
    appears in the normalized agenda source. Returns list of warning strings
    for amounts that could not be verified.
    """
    # Build source corpus from all agenda items
    source_parts = []
    for item in meeting.get("data", {}).get("agendaItems", []):
        for field in ("description", "staffRecommendation", "fiscalAmounts"):
            val = item.get(field)
            if val:
                source_parts.append(str(val))
    source_text = " ".join(source_parts)
    source_amounts = {_normalize_amount(m) for m in _DOLLAR_PATTERN.findall(source_text)}

    # Extract amounts from generated briefing text
    generated_text = json.dumps(briefing.get("data", {}))
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
    constituent: Optional[dict] = None,
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
            "card": {
                "headline": card.headline,
                "whatYouNeedToDo": card.whatYouNeedToDo,
                "askThisInTheRoom": card.askThisInTheRoom,
                "tryThis": card.tryThis,
                "actionButtons": [],
            },
        }

        if detail:
            supporting_docs = list(meeting.get("agendaFiles", []))
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

    print(f"\n  Generating briefing for {city} — {date} ({len(items)} agenda items)")

    if len(items) < 3:
        print(f"  SKIP: Too few agenda items ({len(items)})")
        return {"status": "skipped", "reason": "too_few_items"}

    constituent = load_constituent_data(city_slug, storage, cfg.sources_prefix) if city_slug else None
    if constituent:
        top = format_top_constituent_issues(constituent)
        print(f"  Haystaq data: {constituent.get('voter_count_with_scores', 0):,} voters — top: {top}")
    else:
        print(f"  SKIP: No Haystaq constituent data for {city_slug} — run collect_haystaq_batch.py --from-csv first")
        return {"status": "skipped", "reason": "no_haystaq"}

    if dry_run:
        print(f"  DRY RUN: Would generate briefing for {city} {date}")
        return {"status": "dry_run"}

    gemini = GeminiClient(default_model=GeminiModelType.FLASH)

    # Pass 1: Categorize
    print(f"  Pass 1: Categorizing {len(items)} agenda items...")
    t0 = time.time()
    categorized = pass1_categorize(meeting, gemini, constituent=constituent)
    t1 = time.time()
    priority_count = sum(1 for item in categorized.items if item.isPriority)
    print(f"  Pass 1 done: {len(categorized.items)} items, {priority_count} priorities ({t1-t0:.1f}s)")

    # Pass 2: Generate cards
    print(f"  Pass 2: Generating card content...")
    t0 = time.time()
    cards = pass2_generate_cards(meeting, categorized, gemini, constituent=constituent)
    t1 = time.time()
    print(f"  Pass 2 done: {len(cards.priorityIssues)} priority cards ({t1-t0:.1f}s)")

    # Pass 3: Generate details for each priority
    details = []
    for i, card in enumerate(cards.priorityIssues):
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
            )
            details.append(detail)
            t1 = time.time()
            print(f"  Pass 3.{i+1} done ({t1-t0:.1f}s)")

    # Provenance check
    source_texts = {}
    for card in cards.priorityIssues:
        raw_items = meeting.get("data", {}).get("agendaItems", [])
        raw_item = next((it for it in raw_items if (it.get("title") or "").lower().strip() == card.agendaItemTitle.lower().strip()), None)
        if raw_item:
            source_texts[card.agendaItemTitle] = " ".join(filter(None, [
                raw_item.get("description"), raw_item.get("staffRecommendation")
            ]))
    provenance_warnings = check_provenance(details, source_texts)
    if provenance_warnings:
        for w in provenance_warnings:
            print(f"  ⚠ Provenance: {w}")

    # Assemble
    print(f"  Assembling briefing...")
    briefing = assemble_briefing(meeting, categorized, cards, details, constituent=constituent)
    if provenance_warnings:
        briefing["provenanceWarnings"] = provenance_warnings

    # Fiscal cross-check
    fiscal_warnings = check_fiscal_amounts(briefing, meeting)
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

    # Save
    safe_name = f"{city_slug}_{date}_briefing.json"
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


def main():
    parser = argparse.ArgumentParser(description="Generate council meeting briefings")
    parser.add_argument("--file", help="Storage key for a normalized meeting JSON (e.g. meeting_pipeline/output/normalized/johnstown-OH_2026-04-07.json)")
    parser.add_argument("--city", help="City slug (e.g. johnstown-OH) — uses most recent normalized file")
    parser.add_argument("--batch", action="store_true", help="Generate for all normalized meetings")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be generated, no LLM calls")
    args = parser.parse_args()

    if not args.file and not args.city and not args.batch:
        parser.error("Specify --file, --city, or --batch")

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    if args.file:
        target_keys = [args.file]
    elif args.city:
        all_keys = storage.list_keys(normalized_prefix)
        city_slug = args.city.lower().replace(" ", "-")
        matches = sorted(
            k for k in all_keys
            if k.split("/")[-1].lower().startswith(city_slug) and k.endswith(".json")
        )
        if not matches:
            print(f"No normalized files found for city: {args.city}")
            print(f"  Looked in: {normalized_prefix}")
            sys.exit(1)
        target_keys = [matches[-1]]
    else:
        all_keys = storage.list_keys(normalized_prefix)
        # Only match {city-slug}_{YYYY-MM-DD}.json — skip combined dumps like normalized_meetings.json
        target_keys = sorted(
            k for k in all_keys
            if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k)
        )
        # Skip files that already have a briefing (batch mode default)
        briefing_prefix = f"{cfg.output_prefix}/briefings"
        existing_briefing_keys = set(storage.list_keys(briefing_prefix))
        def _has_briefing(norm_key: str) -> bool:
            fn = norm_key.split("/")[-1]           # e.g. chapel-hill-NC_2026-04-15.json
            stem = fn[:-5]                          # chapel-hill-NC_2026-04-15
            city_date = stem                        # same
            return any(city_date in bk for bk in existing_briefing_keys)
        before = len(target_keys)
        target_keys = [k for k in target_keys if not _has_briefing(k)]
        skipped_existing = before - len(target_keys)
        if skipped_existing:
            print(f"Skipping {skipped_existing} files with existing briefings (--force to regenerate all)")
        if not target_keys:
            print(f"No normalized meeting files found in {normalized_prefix}")
            sys.exit(1)

    results = []
    for key in target_keys:
        filename = key.split("/")[-1]
        print(f"\nProcessing: {filename}")
        try:
            result = generate_briefing_for_meeting(key, storage, cfg, dry_run=args.dry_run)
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            result = {"status": "error", "error": str(e), "cost": 0}
        result["file"] = filename
        results.append(result)

    if len(results) > 1:
        print(f"\n{'=' * 60}")
        print("BRIEFING GENERATION SUMMARY")
        print(f"{'=' * 60}")
        for r in results:
            status = r.get("status", "?")
            cost = r.get("cost", 0)
            fiscal_warns = len(r.get("fiscal_warnings", []))
            warn_str = f" ⚠ {fiscal_warns} fiscal" if fiscal_warns else ""
            print(f"  {r.get('file', '?'):40s} [{status}]{warn_str} ${cost:.4f}")
        total_cost = sum(r.get("cost", 0) for r in results)
        ok_count = sum(1 for r in results if r.get("status") == "ok")
        error_count = sum(1 for r in results if r.get("status") == "error")
        no_haystaq_count = sum(1 for r in results if r.get("reason") == "no_haystaq")
        skipped_count = sum(1 for r in results if r.get("status") in ("skipped", "dry_run")) - no_haystaq_count
        print(f"\n  Total: {ok_count}/{len(results)} generated, {error_count} errors, {skipped_count} skipped, ${total_cost:.4f}")
        if no_haystaq_count:
            print(f"  Skipped (no Haystaq): {no_haystaq_count} — run: collect_haystaq_batch.py --from-csv --skip-existing")

        # Write structured run log to storage
        run_log = {
            "run_at": datetime.utcnow().isoformat() + "Z",
            "total": len(results),
            "ok": ok_count,
            "errors": error_count,
            "skipped": skipped_count,
            "skipped_no_haystaq": no_haystaq_count,
            "total_cost_usd": round(total_cost, 6),
            "briefings": [
                {
                    "file": r.get("file"),
                    "status": r.get("status"),
                    "cost_usd": round(r.get("cost", 0), 6),
                    "fiscal_warnings": r.get("fiscal_warnings", []),
                    "error": r.get("error"),
                }
                for r in results
            ],
        }
        log_key = f"{cfg.output_prefix}/run_logs/briefing_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        storage.write_json(log_key, run_log)
        print(f"  Run log: {log_key}")


if __name__ == "__main__":
    main()
