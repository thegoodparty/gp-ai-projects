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
        return None
    data = storage.read_json(key)
    return data if data.get("issues") else None


def format_constituent_context(constituent: dict) -> str:
    """Format constituent data into a text block for LLM prompts."""
    issues = constituent.get("issues", [])
    if not issues:
        return ""

    lines = ["CONSTITUENT DATA (Haystaq voter modeling scores, 0-100 scale):"]
    voter_count = constituent.get("voter_count_with_scores", 0)
    if voter_count:
        lines.append(f"  Based on {voter_count:,} registered voters")

    tiers = {"Critical": [], "Strong": [], "Moderate": [], "Lower": []}
    for issue in issues:
        tier = issue.get("tier_label", "Lower")
        tiers.setdefault(tier, []).append(issue)

    for tier_name in ["Critical", "Strong", "Moderate", "Lower"]:
        tier_issues = tiers.get(tier_name, [])
        if tier_issues:
            lines.append(f"\n  {tier_name} constituent priorities:")
            for issue in tier_issues:
                lines.append(f"    - {issue['name']}: {issue['score']:.0f}/100")

    context = constituent.get("context_scores", {})
    if context:
        lines.append(f"\n  Ideological context:")
        for name, score in context.items():
            lines.append(f"    - {name}: {score:.0f}/100")

    return "\n".join(lines)


def format_top_constituent_issues(constituent: dict, n: int = 5) -> str:
    """Format just the top N constituent issues as a compact summary."""
    issues = constituent.get("issues", [])
    if not issues:
        return ""
    top = sorted(issues, key=lambda i: i.get("score", 0), reverse=True)[:n]
    return ", ".join(f"{i['name']} ({i['score']:.0f}/100)" for i in top)


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


def pass3_generate_detail(
    meeting: dict,
    card: PriorityIssueCard,
    categorized_item: CategorizedAgendaItem,
    all_items: list[CategorizedAgendaItem],
    gemini,
    constituent: Optional[dict] = None,
    available_docs: Optional[list[dict]] = None,
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
        print(f"  Haystaq data: not available (generic briefing)")

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
            )
            details.append(detail)
            t1 = time.time()
            print(f"  Pass 3.{i+1} done ({t1-t0:.1f}s)")

    # Assemble
    print(f"  Assembling briefing...")
    briefing = assemble_briefing(meeting, categorized, cards, details, constituent=constituent)

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

    return {"status": "ok", "output": output_key, "cost": cost}


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
            print(f"  {r.get('file', '?'):40s} [{status}] ${cost:.4f}")
        total_cost = sum(r.get("cost", 0) for r in results)
        ok_count = sum(1 for r in results if r.get("status") == "ok")
        print(f"\n  Total: {ok_count}/{len(results)} generated, ${total_cost:.4f}")


if __name__ == "__main__":
    main()
