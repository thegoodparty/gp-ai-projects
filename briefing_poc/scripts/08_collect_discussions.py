"""
08_collect_discussions.py — Collect discussion narratives for contentious council items.

This script identifies the most significant/contentious items from the council's
recent agenda (using Pass 1 and Pass 2 analysis data), researches each one via
web search, and extracts structured discussion narratives with full source tracing.

Why web search?
Charlotte's Legistar API has empty MinutesNote fields (0% populated) and official
meeting minutes have a ~7-month publishing backlog. Local news coverage (Charlotte
Observer, Charlotte Ledger, WBTV, etc.) is the most practical source for the POC.
For production, this should be supplemented with official minutes from CodeLibrary
(amlegal.com) or Granicus video transcripts.

IMPORTANT — Source tracing for elected officials:
Every claim in the output must be traceable to a source. This script:
  1. Stores raw source excerpts (~2000 chars) alongside URLs
  2. Calculates confidence programmatically (not LLM-determined)
  3. Flags council member positions as directly sourced or inferred
  4. Runs post-extraction verification (name-in-excerpt checks)
See the Verifiability Gaps section in product-direction-reconciliation.md.

Usage:
    python scripts/08_collect_discussions.py

Prerequisites:
    - GEMINI_API_KEY in your .env file
    - Analysis results from 04_run_analysis.py in data/analysis/
    - (Optional) TAVILY_API_KEY for fallback search

Output:
    data/discussions/discussion_narratives.json
"""

# --- Standard library imports ---
import json
from pathlib import Path
import time
import sys
import os
import re
import asyncio
from datetime import date

# --- Third-party imports ---
import httpx
from pydantic import BaseModel
from dotenv import load_dotenv

# --- Project imports ---
from shared.llm_gemini import GeminiClient, GeminiModelType
from city_config import cfg
from utils import load_json

load_dotenv()


# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir
ANALYSIS_DIR = DATA_DIR / "analysis"
LEGISTAR_DIR = DATA_DIR / "legistar"
OUTPUT_DIR = DATA_DIR / "discussions"

MODEL = GeminiModelType.FLASH
TEMPERATURE = 0.2
THINKING_BUDGET = 0  # No internal thinking needed for search extraction.

# How many items to research. Loaded from city_config.json.
MAX_ITEMS = cfg.discussion_max_items

# Legistar gateway URL pattern. Loaded from city_config.json.
LEGISTAR_GATEWAY_URL = cfg.legistar_gateway_pattern

# Items to skip — these are procedural, not substantive debates.
SKIP_PATTERNS = [
    "Closed Session",
    "Deferrals / Withdrawals",
    "Consent agenda items",
    "BUSINESS",
]

# Max characters to store from each source article.
RAW_EXCERPT_MAX_CHARS = 2000

# Delay between web search calls to avoid rate limiting.
SEARCH_DELAY_SECONDS = 2


# ============================================================================
# PYDANTIC MODELS — Structured output with full source tracing
# ============================================================================

class SourceReference(BaseModel):
    """A traceable, verifiable source for claims in the discussion narrative."""
    title: str             # Article/page title
    url: str               # Full URL to the source
    publication: str       # e.g., "Charlotte Observer", "Charlotte Ledger", "WBTV"
    date: str = ""         # Publication date if available
    source_type: str       # "news_article" | "meeting_minutes" | "government_document" | "other"
    raw_excerpt: str = ""  # Stored excerpt (~2000 chars) for offline verification
    verified: bool = False # True if we fetched and stored the raw content


class CouncilMemberPosition(BaseModel):
    """A council member's stated position, with attribution."""
    name: str                          # "Council Member Driggs"
    position: str                      # "support" | "oppose" | "mixed" | "procedural"
    summary: str                       # 1-2 sentence reasoning — uses hedged language
    source_title: str = ""             # Which source this came from
    is_directly_sourced: bool = False  # True only if a source explicitly names this position
    direct_quote: str = ""             # Actual quote from the source, if available


class DiscussionNarrative(BaseModel):
    """Discussion context for a single legislative item, with full provenance."""
    item_title: str
    legistar_url: str = ""
    meeting_date: str = ""
    outcome: str = ""
    narrative_summary: str = ""
    key_arguments_for: list[str] = []
    key_arguments_against: list[str] = []
    council_positions: list[CouncilMemberPosition] = []
    public_sentiment: str = ""
    sources: list[SourceReference] = []
    confidence: str = "low"            # Calculated programmatically, not by LLM


# LLM extraction schema — simpler than the full model, used for structured extraction.
class LLMExtractedNarrative(BaseModel):
    """What the LLM extracts from search results. We post-process this."""
    narrative_summary: str
    meeting_date: str = ""
    outcome: str = ""
    key_arguments_for: list[str] = []
    key_arguments_against: list[str] = []
    council_positions: list[CouncilMemberPosition] = []
    public_sentiment: str = ""


class DiscussionCollection(BaseModel):
    """Complete output with provenance and verification metadata."""
    items_researched: int
    items_with_narratives: int
    total_sources_found: int
    total_sources_verified: int
    collection_date: str
    search_method: str
    narratives: list[DiscussionNarrative]
    items_without_coverage: list[str]


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def build_title_to_url() -> dict[str, str]:
    """Build a mapping from matter titles to Legistar gateway URLs."""
    matters_path = LEGISTAR_DIR / "matters.json"
    if not matters_path.exists():
        return {}
    with open(matters_path, "r", encoding="utf-8") as f:
        matters = json.load(f)
    mapping = {}
    for matter in matters:
        title = matter.get("MatterTitle", "")
        matter_id = matter.get("MatterId")
        if title and matter_id:
            mapping[title] = LEGISTAR_GATEWAY_URL.format(matter_id=matter_id)
    return mapping


def should_skip(title: str) -> bool:
    """Check if an item title matches a skip pattern (procedural items)."""
    for pattern in SKIP_PATTERNS:
        if pattern.lower() in title.lower():
            return True
    return False


def extract_title_from_dissent(item: str) -> str:
    """Extract the title from a dissent_items string like 'ITEM 47: Title Here'."""
    if ": " in item:
        return item.split(": ", 1)[1]
    return item


def select_target_items(pass1: dict, pass2: dict) -> list[dict]:
    """
    Select the 15-20 most significant items to research.

    Tier 1: Non-unanimous votes on substantive items (from pass2.dissent_items).
    Tier 2: Key motions not already in Tier 1 (from pass2.key_motions).

    Returns a list of dicts with 'title' and 'tier' keys.
    """
    items = []
    seen_titles = set()

    # Tier 1: Dissent items (non-unanimous votes).
    for item_str in pass2.get("dissent_items", []):
        title = extract_title_from_dissent(item_str)
        if should_skip(title):
            continue
        if title not in seen_titles:
            seen_titles.add(title)
            items.append({"title": title, "tier": 1})

    # Tier 2: Key motions not already covered.
    for motion in pass2.get("key_motions", []):
        if motion not in seen_titles and not should_skip(motion):
            seen_titles.add(motion)
            items.append({"title": motion, "tier": 2})

    # Cap at MAX_ITEMS, prioritizing Tier 1.
    items.sort(key=lambda x: x["tier"])
    return items[:MAX_ITEMS]


def simplify_title(title: str) -> str:
    """
    Simplify a legislative title for web search.

    Strips boilerplate prefixes and extracts the distinctive part.
    "Rezoning Petition: 2025-118 by Charlotte Planning..." → "Charlotte rezoning 2025-118"
    """
    # Strip common prefixes.
    for prefix in ["Rezoning Petition: ", "Public Hearing and Decision on the "]:
        if title.startswith(prefix):
            title = title[len(prefix):]

    # Truncate long titles to first 80 chars.
    if len(title) > 80:
        title = title[:80]

    return title


async def fetch_raw_excerpt(url: str) -> str:
    """
    Fetch a raw text excerpt from a URL for offline verification.

    Returns up to RAW_EXCERPT_MAX_CHARS of the page content.
    Returns empty string on failure (non-blocking — we don't want
    a single failed fetch to stop the pipeline).
    """
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; GoodPartyBot/1.0; research)"
                },
            )
            if response.status_code == 200:
                # Strip HTML tags for a rough text extraction.
                text = re.sub(r"<[^>]+>", " ", response.text)
                text = re.sub(r"\s+", " ", text).strip()
                return text[:RAW_EXCERPT_MAX_CHARS]
    except Exception:
        pass
    return ""


def calculate_confidence(sources: list[SourceReference]) -> str:
    """
    Calculate confidence programmatically based on source quality.

    High:   2+ verified sources with raw excerpts
    Medium: 1 verified source with raw excerpt
    Low:    0 verified sources
    """
    verified_count = sum(1 for s in sources if s.verified and s.raw_excerpt)
    if verified_count >= 2:
        return "high"
    elif verified_count >= 1:
        return "medium"
    return "low"


def _parse_extracted(extracted) -> dict:
    """Normalize LLM extraction result (Pydantic model or dict) to a plain dict."""
    if isinstance(extracted, dict):
        return extracted
    return extracted.model_dump() if hasattr(extracted, "model_dump") else dict(extracted)


def _build_narrative(
    item_title: str,
    legistar_url: str,
    extracted_data: dict,
    sources: list[SourceReference],
) -> DiscussionNarrative:
    """Build a verified DiscussionNarrative from extracted data and sources."""
    narrative = DiscussionNarrative(
        item_title=item_title,
        legistar_url=legistar_url,
        meeting_date=extracted_data.get("meeting_date", ""),
        outcome=extracted_data.get("outcome", ""),
        narrative_summary=extracted_data.get("narrative_summary", ""),
        key_arguments_for=extracted_data.get("key_arguments_for", []),
        key_arguments_against=extracted_data.get("key_arguments_against", []),
        council_positions=[
            CouncilMemberPosition(**pos) if isinstance(pos, dict) else pos
            for pos in extracted_data.get("council_positions", [])
        ],
        public_sentiment=extracted_data.get("public_sentiment", ""),
        sources=sources,
        confidence=calculate_confidence(sources),
    )
    return verify_positions(narrative)


def verify_positions(
    narrative: DiscussionNarrative,
) -> DiscussionNarrative:
    """
    Post-extraction verification: check that council member names
    actually appear in at least one raw source excerpt.

    If a name doesn't appear in any source, set is_directly_sourced=False
    and clear the direct_quote.
    """
    # Collect all raw excerpts into one searchable block.
    all_text = " ".join(
        s.raw_excerpt for s in narrative.sources if s.raw_excerpt
    ).lower()

    if not all_text:
        # No source text available — mark all positions as not directly sourced.
        for pos in narrative.council_positions:
            pos.is_directly_sourced = False
            pos.direct_quote = ""
        return narrative

    for pos in narrative.council_positions:
        # Extract the last name from "Council Member Driggs" → "driggs".
        name_parts = pos.name.lower().split()
        last_name = name_parts[-1] if name_parts else ""

        if last_name and last_name in all_text:
            # Name found in source — position may be directly sourced.
            # (The LLM's is_directly_sourced flag is still more conservative.)
            pass
        else:
            # Name NOT found in any source — cannot be directly sourced.
            pos.is_directly_sourced = False
            pos.direct_quote = ""

    return narrative


# ============================================================================
# RESEARCH FUNCTIONS
# ============================================================================

def research_item(
    client: GeminiClient,
    item_title: str,
    legistar_url: str,
) -> DiscussionNarrative:
    """
    Research a single council item using Gemini's grounded search.

    Steps:
    1. Call generate_with_search to find web coverage
    2. Fetch raw excerpts from source URLs
    3. Extract structured narrative via generate_structured_content
    4. Calculate confidence and run verification
    """
    simplified = simplify_title(item_title)

    search_prompt = f"""Research the {cfg.city_name_full} {cfg.governing_body}'s discussion and debate about this
specific agenda item. Focus on what council members said, arguments for and against,
public comment, and the political dynamics.

ITEM TITLE: {item_title}
CITY: {cfg.city_name_full}
TIME PERIOD: {cfg.data_period}

Find:
1. What happened — was it approved, denied, deferred, modified?
2. Key arguments FOR the item
3. Key arguments AGAINST the item or concerns raised
4. Which specific council members took positions and what did they say?
5. Public reaction or community sentiment
6. Any related context (prior history, community impact)

Search for: {cfg.city_name} {cfg.state_code.upper()} council "{simplified}" discussion debate vote
Focus on: {cfg.discussion_search_outlets}, and other local {cfg.city_name} media.

If you cannot find specific discussion content about this exact item, state that clearly."""

    # Step 1: Grounded search.
    search_result = client.generate_with_search(
        prompt=search_prompt,
        temperature=TEMPERATURE,
        thinking_budget=THINKING_BUDGET,
        trace_name=f"search_{simplified[:30]}",
    )

    raw_text = search_result.get("text", "")
    grounding_sources = search_result.get("sources", [])

    # Step 2: Fetch raw excerpts from source URLs.
    sources: list[SourceReference] = []
    for gs in grounding_sources:
        url = gs.get("uri", "")
        title = gs.get("title", "Unknown")
        if not url or url == "Unknown":
            continue

        # Fetch raw excerpt for verification.
        raw_excerpt = asyncio.run(fetch_raw_excerpt(url))

        # Infer publication from URL domain using city_config.json mappings.
        publication = "Unknown"
        domain = url.split("//")[-1].split("/")[0].lower()
        for domain_key, pub_name in cfg.discussion_news_domains.items():
            if domain_key in domain:
                publication = pub_name
                break
        if publication == "Unknown" and "gov" in domain:
            publication = "Government Source"

        sources.append(SourceReference(
            title=title,
            url=url,
            publication=publication,
            source_type="news_article",
            raw_excerpt=raw_excerpt,
            verified=bool(raw_excerpt),
        ))

    # Step 3: Extract structured narrative from the search results.
    if raw_text.strip():
        extraction_prompt = f"""You are extracting structured discussion data about a {cfg.city_name_full}
{cfg.governing_body} item from search results. Be conservative and accurate.

IMPORTANT RULES:
- Only include information that the search results actually state.
- For council member positions: use hedged language ("reportedly opposed",
  "expressed concerns about") unless you have a direct quote.
- If a direct quote exists, include it in the direct_quote field with quotation marks.
- Set is_directly_sourced=true ONLY if a source explicitly names the council
  member's position. If you're inferring from a vote tally, set it to false.
- If the search results don't contain relevant discussion content, return
  an empty narrative_summary.

ITEM TITLE: {item_title}

SEARCH RESULTS:
{raw_text}

Extract the discussion narrative, arguments, and council member positions."""

        try:
            extracted = client.generate_structured_content(
                prompt=extraction_prompt,
                response_schema=LLMExtractedNarrative,
                temperature=0.1,
                thinking_budget=0,
                trace_name=f"extract_{simplified[:30]}",
            )
            extracted_data = _parse_extracted(extracted)
        except Exception as e:
            print(f"    Extraction failed: {e}")
            extracted_data = {"narrative_summary": ""}
    else:
        extracted_data = {"narrative_summary": ""}

    return _build_narrative(item_title, legistar_url, extracted_data, sources)


# ============================================================================
# TAVILY FALLBACK
# ============================================================================

def try_tavily_fallback(
    gemini_client: GeminiClient,
    item_title: str,
    legistar_url: str,
) -> DiscussionNarrative | None:
    """
    Fallback search using Tavily when Gemini grounding returns thin results.
    Returns None if Tavily is not available or finds nothing.
    """
    try:
        from shared.tavily_client import SharedTavilyClient
    except ImportError:
        return None

    try:
        tavily = SharedTavilyClient()
    except ValueError:
        # TAVILY_API_KEY not set.
        return None

    simplified = simplify_title(item_title)
    query = f"{cfg.city_name} {cfg.state_code.upper()} city council {simplified} discussion debate vote"

    try:
        # Tavily search is async — wrap in asyncio.run().
        result = asyncio.run(tavily.search(
            query=query,
            search_depth="advanced",
            topic="news",
            max_results=5,
            include_domains=cfg.discussion_tavily_domains,
        ))
    except Exception as e:
        print(f"    Tavily search failed: {e}")
        return None

    tavily_results = result.get("results", [])
    if not tavily_results:
        return None

    # Build sources from Tavily results.
    sources: list[SourceReference] = []
    context_parts = []
    for tr in tavily_results:
        content = tr.get("content", "")
        raw_content = tr.get("raw_content", content)
        url = tr.get("url", "")
        title = tr.get("title", "Unknown")

        # Infer publication from URL.
        publication = "Unknown"
        if url:
            domain = url.split("//")[-1].split("/")[0].lower()
            for key, name in cfg.discussion_news_domains.items():
                if key in domain:
                    publication = name
                    break

        raw_excerpt = (raw_content or content)[:RAW_EXCERPT_MAX_CHARS]
        sources.append(SourceReference(
            title=title,
            url=url,
            publication=publication,
            source_type="news_article",
            raw_excerpt=raw_excerpt,
            verified=bool(raw_excerpt),
        ))
        context_parts.append(f"Source: {title}\nContent: {content[:500]}")

    # Extract structured narrative from Tavily results.
    context_text = "\n\n".join(context_parts)
    extraction_prompt = f"""Extract discussion data about this {cfg.city_name_full} {cfg.governing_body} item
from the search results below. Be conservative — only include what the sources actually say.

IMPORTANT: Use hedged language for council member positions. Set is_directly_sourced=true
ONLY if a source explicitly names the member's stance.

ITEM: {item_title}

SEARCH RESULTS:
{context_text}"""

    try:
        extracted = gemini_client.generate_structured_content(
            prompt=extraction_prompt,
            response_schema=LLMExtractedNarrative,
            temperature=0.1,
            thinking_budget=0,
            trace_name=f"tavily_extract_{simplified[:20]}",
        )
        extracted_data = _parse_extracted(extracted)
    except Exception:
        extracted_data = {"narrative_summary": ""}

    return _build_narrative(item_title, legistar_url, extracted_data, sources)


# ============================================================================
# MAIN
# ============================================================================

def collect_discussions():
    """
    Main entry point: identify target items, research each one, save results.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not os.getenv("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found in environment.")
        sys.exit(1)

    print("=" * 60)
    print(f"{cfg.city_name} POC — Discussion Narrative Collection")
    print("=" * 60)
    print()

    # ------------------------------------------------------------------
    # 1. Load analysis data for item selection
    # ------------------------------------------------------------------
    print("Loading analysis data...")
    pass1 = load_json(ANALYSIS_DIR / "pass1_legislative_overview.json")
    pass2 = load_json(ANALYSIS_DIR / "pass2_vote_analysis.json")

    if not pass1 or not pass2:
        print("ERROR: Missing pass1 or pass2 analysis data.")
        print("Run 04_run_analysis.py first.")
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Select target items
    # ------------------------------------------------------------------
    print("Selecting target items...")
    targets = select_target_items(pass1, pass2)
    print(f"  Selected {len(targets)} items to research")
    for i, t in enumerate(targets, 1):
        print(f"    {i}. [Tier {t['tier']}] {t['title'][:70]}")
    print()

    # ------------------------------------------------------------------
    # 3. Build title→URL mapping for Legistar links
    # ------------------------------------------------------------------
    title_to_url = build_title_to_url()
    print(f"  Legistar URL mapping: {len(title_to_url)} matters")
    print()

    # ------------------------------------------------------------------
    # 4. Research each item
    # ------------------------------------------------------------------
    print("Researching items via web search...")
    start_time = time.time()

    client = GeminiClient(
        default_model=MODEL,
        default_temperature=TEMPERATURE,
        thinking_budget=THINKING_BUDGET,
        max_connections=5,
        max_keepalive_connections=3,
        max_retries=3,
    )

    narratives: list[DiscussionNarrative] = []
    items_without_coverage: list[str] = []
    search_method = "gemini_grounded_search"

    for i, target in enumerate(targets, 1):
        title = target["title"]
        legistar_url = title_to_url.get(title, "")
        print(f"  [{i}/{len(targets)}] {title[:60]}...")

        try:
            narrative = research_item(client, title, legistar_url)
        except Exception as e:
            print(f"    ERROR: {e}")
            narrative = DiscussionNarrative(
                item_title=title,
                legistar_url=legistar_url,
                confidence="low",
            )

        # If low confidence from Gemini, try Tavily fallback.
        if narrative.confidence == "low" and not narrative.narrative_summary:
            print(f"    Low confidence — trying Tavily fallback...")
            tavily_narrative = try_tavily_fallback(client, title, legistar_url)
            if tavily_narrative and tavily_narrative.confidence != "low":
                narrative = tavily_narrative
                search_method = "mixed"
                print(f"    Tavily found content (confidence: {narrative.confidence})")
            else:
                print(f"    No coverage found")

        # Track results.
        if narrative.narrative_summary and narrative.confidence != "low":
            narratives.append(narrative)
            src_count = len(narrative.sources)
            verified = sum(1 for s in narrative.sources if s.verified)
            print(f"    {narrative.confidence} confidence, {src_count} sources ({verified} verified)")
        else:
            items_without_coverage.append(title)
            narratives.append(narrative)  # Keep low-confidence items in the data for reference.
            print(f"    No substantive coverage found")

        # Rate limit delay between searches.
        if i < len(targets):
            time.sleep(SEARCH_DELAY_SECONDS)

    elapsed = time.time() - start_time
    stats = client.get_usage_stats()

    print()

    # ------------------------------------------------------------------
    # 5. Save results
    # ------------------------------------------------------------------
    print("Saving discussion narratives...")

    total_sources = sum(len(n.sources) for n in narratives)
    total_verified = sum(
        sum(1 for s in n.sources if s.verified)
        for n in narratives
    )
    items_with_narratives = sum(
        1 for n in narratives
        if n.narrative_summary and n.confidence != "low"
    )

    collection = DiscussionCollection(
        items_researched=len(targets),
        items_with_narratives=items_with_narratives,
        total_sources_found=total_sources,
        total_sources_verified=total_verified,
        collection_date=date.today().strftime("%Y-%m-%d"),
        search_method=search_method,
        narratives=narratives,
        items_without_coverage=items_without_coverage,
    )

    output_path = OUTPUT_DIR / "discussion_narratives.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(collection.model_dump(), f, indent=2, ensure_ascii=False)

    print(f"  Saved: {output_path.name}")
    print()

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print("=" * 60)
    print("Discussion Narrative Collection Complete!")
    print("=" * 60)
    print(f"  Items researched:     {len(targets)}")
    print(f"  Items with coverage:  {items_with_narratives}")
    print(f"  Items without:        {len(items_without_coverage)}")
    print(f"  Total sources found:  {total_sources}")
    print(f"  Sources verified:     {total_verified}")
    print(f"  Search method:        {search_method}")
    print(f"  Time:                 {elapsed:.1f}s")
    print(f"  LLM cost:             ${stats['total_cost']:.4f}")
    print(f"  Tokens:               {stats['total_prompt_tokens']:,} prompt + {stats['total_completion_tokens']:,} completion")
    print()

    if items_without_coverage:
        print("Items without coverage:")
        for title in items_without_coverage:
            print(f"  - {title[:70]}")

    print("=" * 60)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    collect_discussions()
