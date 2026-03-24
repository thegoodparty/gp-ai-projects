"""
05_assemble_briefing.py — Assemble final briefing document from analysis results.

This script takes the 6 analysis pass outputs from 04_run_analysis.py and assembles
them into a single, polished markdown briefing document. It uses one final LLM call
to transform the structured JSON data into natural, readable prose that a newly
elected council member could read and immediately understand.

Why one more LLM call?
The analysis passes produced structured JSON — great for programmatic use but not
for reading. This final step converts that data into well-written sections with
natural transitions, editorial judgment about emphasis, and a coherent narrative arc
from "here's the big picture" down to "here's what you should do Monday morning."

Output structure:
  1. Executive Summary (3-5 paragraphs)
  2. Key Themes (the big issues cutting across everything)
  3. Your Constituents (demographics, party, ideology, issue priorities)
  4. Council vs. Constituent Priorities (mismatch/gap analysis)
  5. Quick Wins (concrete actions tied to constituent gaps)
  6. Your First 90 Days (immediate priorities, informed by constituent gaps)
  7. Legislative Landscape (topics, patterns, volume)
  8. Budget & Fiscal Context (revenue, spending, tax trends)
  9. Voting & Decision Patterns (who moves what, consensus vs. dissent)
  10. Committee Guide (which committees do what)
  11. Key Documents Reference (summaries of the most important staff reports)
  12. Ongoing Issues to Track
  13. Key Relationships to Build
  14. Appendix: Data Sources & Methodology

Usage:
    python scripts/05_assemble_briefing.py

Prerequisites:
    - GEMINI_API_KEY in your .env file
    - Analysis results from 04_run_analysis.py in data/analysis/
    - (Optional) Constituent data from 06_collect_constituent_data.py in data/constituent/
    - (Optional) Mismatch analysis from 07_council_vs_constituent.py in data/analysis/

Output:
    data/briefing/charlotte_council_briefing.md
    data/briefing/briefing_metadata.json
"""

# --- Standard library imports ---
import json
from pathlib import Path
import time
import sys
import os
from datetime import date

# difflib provides sequence-matching algorithms for comparing strings.
# We use it to fuzzy-match matter titles from LLM output back to exact titles
# in our source data, since the LLM sometimes paraphrases or truncates titles.
# Part of Python's standard library — no pip install needed.
import difflib

# --- Third-party imports ---
from dotenv import load_dotenv

# --- Project imports ---
from shared.llm_gemini import GeminiClient, GeminiModelType
from city_config import cfg
from utils import load_json

load_dotenv()


# ============================================================================
# CONFIGURATION
# ============================================================================

# Where analysis results live (output of 04_run_analysis.py).
ANALYSIS_DIR = cfg.data_dir / "analysis"

# Where constituent data lives (output of 06_collect_constituent_data.py).
CONSTITUENT_DIR = cfg.data_dir / "constituent"

# Where discussion narratives live (output of 08_collect_discussions.py).
DISCUSSION_DIR = cfg.data_dir / "discussions"

# Where to save the final briefing.
OUTPUT_DIR = cfg.data_dir / "briefing"

# Model choice: Flash for speed and cost, with dynamic thinking for quality.
MODEL = GeminiModelType.FLASH
TEMPERATURE = 0.3  # Slightly creative for better prose, but still factual.


# ============================================================================
# SOURCE CITATION CONFIGURATION
# ============================================================================

# Where raw Legistar data lives (for building title→URL lookups).
DATA_DIR = cfg.data_dir
LEGISTAR_DIR = DATA_DIR / "legistar"

# Legistar gateway URL — accepts the API's MatterId and redirects to the
# correct LegislationDetail.aspx page on the web. Loaded from city_config.json.
LEGISTAR_GATEWAY_URL = cfg.legistar_gateway_pattern

# Budget dataset URLs for fiscal data citations. Loaded from city_config.json.
LINC_GOVERNMENT_URL = cfg.budget_government_url
LINC_PROPERTY_TAX_URL = cfg.budget_property_tax_url

# Minimum similarity ratio for fuzzy title matching (0.0 to 1.0).
# 0.75 is generous enough to catch LLM paraphrases like truncation or
# minor word changes, but strict enough to avoid false matches.
FUZZY_MATCH_THRESHOLD = 0.75


# ============================================================================
# SOURCE CITATION HELPERS
# ============================================================================

def build_source_registry() -> dict:
    """
    Build a registry mapping matter titles and document filenames to source URLs.

    This scans the raw Legistar data we collected in step 01 and creates two
    lookup dictionaries:
      - title_to_url: maps each MatterTitle → Legistar gateway URL
      - filename_to_url: maps each extracted doc filename → Legistar gateway URL

    Why a registry?
    The LLM analysis passes output matter *titles* (plain strings), but not IDs
    or URLs. To add citations, we need to reverse-lookup: given a title the LLM
    mentions, find the URL a reader can click to verify the source.

    Returns:
        A dict with keys: "title_to_url", "filename_to_url", "matter_count",
        and "budget_urls" (static LINC links).
    """
    # Load all 686 matters from the raw Legistar collection.
    matters_path = LEGISTAR_DIR / "matters.json"
    if not matters_path.exists():
        print("  WARNING: matters.json not found — citations will be unavailable")
        return {"title_to_url": {}, "filename_to_url": {}, "matter_count": 0, "budget_urls": {}}

    with open(matters_path, "r", encoding="utf-8") as f:
        matters = json.load(f)

    # Build title → URL mapping.
    # Each matter has a MatterId we can plug into the gateway URL template.
    title_to_url = {}
    for matter in matters:
        title = matter.get("MatterTitle", "")
        matter_id = matter.get("MatterId")
        if title and matter_id:
            # .format() replaces {matter_id} in the URL template with the actual ID.
            url = LEGISTAR_GATEWAY_URL.format(matter_id=matter_id)
            title_to_url[title] = url

    # Build filename → URL mapping for extracted documents.
    # Our extraction script (03) names files as: {MatterId}_{AttachmentId}.json
    # We can parse out the MatterId from the filename to build the URL.
    filename_to_url = {}
    extracted_dir = DATA_DIR / "extracted"
    if extracted_dir.exists():
        for filepath in extracted_dir.glob("*.json"):
            # Filename pattern: "29026_12345.json" — split on underscore to get MatterId.
            parts = filepath.stem.split("_")
            if len(parts) >= 2 and parts[0].isdigit():
                matter_id = parts[0]
                url = LEGISTAR_GATEWAY_URL.format(matter_id=matter_id)
                filename_to_url[filepath.name] = url

    # Static budget/fiscal URLs — these don't change, they just link to the
    # LINC datasets filtered for Charlotte.
    budget_urls = {
        "government_fiscal": LINC_GOVERNMENT_URL,
        "property_tax_rate": LINC_PROPERTY_TAX_URL,
    }

    print(f"  Source registry: {len(title_to_url)} matter URLs, {len(filename_to_url)} document URLs")

    return {
        "title_to_url": title_to_url,
        "filename_to_url": filename_to_url,
        "matter_count": len(title_to_url),
        "budget_urls": budget_urls,
    }


def fuzzy_match_title(query: str, title_to_url: dict) -> str | None:
    """
    Find the URL for a matter title, allowing fuzzy matching.

    The LLM often paraphrases, truncates, or slightly rewords matter titles.
    For example, it might output "Affordable Housing Support" when the actual
    title is "Affordable Housing Development Support for Five Points Land
    Acquisition." This function handles that by:
      1. Trying an exact match first (fast, O(1) dict lookup)
      2. Falling back to difflib fuzzy matching (slower but handles paraphrases)

    Args:
        query: The title string from the LLM output.
        title_to_url: The dict mapping exact titles → URLs.

    Returns:
        The URL string if a match is found, or None if no match exceeds
        the FUZZY_MATCH_THRESHOLD.
    """
    # Step 1: Exact match — cheapest check, catches most cases.
    if query in title_to_url:
        return title_to_url[query]

    # Step 2: Fuzzy match — uses SequenceMatcher under the hood.
    # get_close_matches() returns a list of the best matches (up to n)
    # that have a similarity ratio >= cutoff.
    # We only need the single best match (n=1).
    matches = difflib.get_close_matches(
        query,                          # The string to match
        title_to_url.keys(),            # The pool of candidates
        n=1,                            # Return at most 1 match
        cutoff=FUZZY_MATCH_THRESHOLD,   # Minimum similarity (0.75 = 75%)
    )

    # If we got a match, return its URL.
    if matches:
        return title_to_url[matches[0]]

    # No match found — the LLM mentioned something we can't trace back.
    return None


def collect_referenced_titles(*pass_dicts: dict) -> set[str]:
    """
    Scan all analysis pass outputs and collect matter titles that are actually
    referenced. This avoids sending all 686 titles to the LLM — we only need
    the ~100-150 titles that appear in the analysis results.

    Why filter?
    Sending all 686 titles bloats the prompt by ~25K tokens, and worse, the
    LLM sometimes tries to dump the entire reference table into a markdown
    table cell. By only including referenced titles, the reference table
    stays small and focused (~5K tokens).

    Args:
        *pass_dicts: Variable number of analysis pass JSON dicts to scan.

    Returns:
        A set of matter title strings found across all passes.
    """
    titles = set()

    for data in pass_dicts:
        if not data:
            continue

        # Pass 1: key_matters in each topic area
        for topic in data.get("topic_areas", []):
            for matter in topic.get("key_matters", []):
                titles.add(matter)

        # Pass 2: key_motions and dissent_items
        for motion in data.get("key_motions", []):
            titles.add(motion)
        for item in data.get("dissent_items", []):
            # Dissent items have format "ITEM 47: Title" — extract the title part.
            if ": " in item:
                titles.add(item.split(": ", 1)[1])

        # Pass 4: document summaries
        for doc in data.get("summaries", []):
            if doc.get("matter_title"):
                titles.add(doc["matter_title"])

        # Pass 5: committee names (not matter titles, but useful for linking)
        for comm in data.get("committees", []):
            if comm.get("name"):
                titles.add(comm["name"])

        # Pass 6: themes may reference specific matters in evidence lists
        for theme in data.get("key_themes", []):
            for ev in theme.get("evidence", []):
                titles.add(ev)

        # Catch-all: immediate_priorities, ongoing_issues, relationships
        for field in ["immediate_priorities", "ongoing_issues", "relationships_to_build"]:
            for item in data.get(field, []):
                titles.add(item)

    return titles


def format_source_reference_table(registry: dict, referenced_titles: set[str], discussions: dict | None = None) -> str:
    """
    Format the source registry as a compact reference table for the LLM prompt.

    Only includes titles that are actually referenced in the analysis passes,
    keeping the table small (~100-150 entries instead of 686). This prevents
    the LLM from being overwhelmed and producing bloated table cells.

    Args:
        registry: The source registry dict from build_source_registry().
        referenced_titles: Set of titles from collect_referenced_titles().
        discussions: Optional discussion narratives JSON from script 08.
                     When provided, news source URLs are appended.

    Returns:
        A formatted string with matched matter URLs, budget URLs, and news URLs.
    """
    lines = [
        "=== SOURCE REFERENCE TABLE ===",
        "Use these URLs to add inline markdown link citations.",
        "Format: [Short descriptive text](URL)",
        "",
        "--- LEGISLATIVE MATTER URLS ---",
    ]

    # For each referenced title, try to find its URL via fuzzy matching.
    matched_count = 0
    for title in sorted(referenced_titles):
        url = fuzzy_match_title(title, registry["title_to_url"])
        if url:
            # Format: "Title | URL" — simple delimiter the LLM can parse.
            lines.append(f"  {title} | {url}")
            matched_count += 1

    lines.append("")
    lines.append("--- BUDGET & FISCAL DATA URLS ---")
    lines.append(f"  Government Fiscal Data (revenues, expenditures) | {registry['budget_urls']['government_fiscal']}")
    lines.append(f"  Property Tax Rate History | {registry['budget_urls']['property_tax_rate']}")
    # Add news/media URLs from discussion narratives (if available).
    news_count = 0
    if discussions and discussions.get("narratives"):
        lines.append("")
        lines.append("--- NEWS & MEDIA URLS ---")
        lines.append("Use these for inline citations in the 'Behind the Votes' section.")
        lines.append("Format: [Publication: Short description](URL)")
        seen_urls = set()
        for narrative in discussions["narratives"]:
            if narrative.get("confidence") not in ("high", "medium"):
                continue
            for source in narrative.get("sources", []):
                url = source.get("url", "")
                if url and source.get("verified") and url not in seen_urls:
                    seen_urls.add(url)
                    title = source.get("title", "Unknown")
                    pub = source.get("publication", "Unknown")
                    lines.append(f"  {title} | {url} | {pub}")
                    news_count += 1

    lines.append("")
    total_label = f"{matched_count} legislative + 2 budget"
    if news_count:
        total_label += f" + {news_count} news/media"
    lines.append(f"Total: {total_label} URLs")
    lines.append("=== END SOURCE REFERENCE TABLE ===")

    return "\n".join(lines)


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def format_vote_summary(pass2: dict) -> str:
    """
    Format vote analysis data into a readable text block for the LLM.

    We extract the key numbers and lists from the structured JSON
    and present them as readable text. This makes the LLM prompt
    clearer and helps it write better prose.

    Args:
        pass2: The pass 2 vote analysis JSON data.

    Returns:
        A formatted string summarizing voting patterns.
    """
    lines = [
        f"Total items with recorded actions: {pass2['total_items_with_actions']}",
        f"Unanimous votes: {pass2['unanimous_count']} ({pass2['unanimous_count'] / max(1, pass2['total_items_with_actions']) * 100:.0f}%)",
        f"Non-unanimous votes: {pass2['non_unanimous_count']}",
        f"Deferred items: {pass2['deferred_count']}",
        f"Denied items: {pass2['denied_count']}",
        "",
        "Key motions:",
    ]

    # Add each key motion as a bullet point.
    for motion in pass2.get("key_motions", []):
        lines.append(f"  - {motion}")

    lines.append("")
    lines.append("Most active council members (motions made, seconded):")

    for mover in pass2.get("frequent_movers", []):
        lines.append(f"  - {mover}")

    lines.append("")
    lines.append("Non-unanimous items (showing some dissent or split votes):")

    # Only show the first 15 dissent items to keep the prompt manageable.
    dissent = pass2.get("dissent_items", [])
    for item in dissent[:15]:
        lines.append(f"  - {item}")
    if len(dissent) > 15:
        lines.append(f"  ... and {len(dissent) - 15} more")

    lines.append("")
    lines.append("Observed patterns:")
    for pattern in pass2.get("patterns", []):
        lines.append(f"  - {pattern}")

    return "\n".join(lines)


def format_budget_summary(pass3: dict) -> str:
    """
    Format budget analysis data into a readable text block.

    Args:
        pass3: The pass 3 budget analysis JSON data.

    Returns:
        A formatted string summarizing fiscal data.
    """
    lines = [
        "FISCAL OVERVIEW:",
        pass3.get("summary", "No summary available."),
        "",
        f"Latest total revenue: ${float(str(pass3.get('total_revenue_latest', 0)).replace(',', '')):,.0f}",
        f"Latest total expenditure: ${float(str(pass3.get('total_expenditure_latest', 0)).replace(',', '')):,.0f}",
        "",
        "REVENUE TRENDS:",
    ]

    for trend in pass3.get("revenue_trends", []):
        lines.append(f"  - {trend}")

    lines.append("")
    lines.append("EXPENDITURE TRENDS:")
    for trend in pass3.get("expenditure_trends", []):
        lines.append(f"  - {trend}")

    lines.append("")
    lines.append(f"TAX RATE ANALYSIS: {pass3.get('tax_rate_analysis', 'N/A')}")
    lines.append("")
    lines.append(f"DEBT ANALYSIS: {pass3.get('debt_analysis', 'N/A')}")

    lines.append("")
    lines.append("FISCAL CONCERNS:")
    for concern in pass3.get("concerns", []):
        lines.append(f"  - {concern}")

    lines.append("")
    lines.append("FISCAL STRENGTHS:")
    for strength in pass3.get("strengths", []):
        lines.append(f"  - {strength}")

    return "\n".join(lines)


def format_committee_summary(pass5: dict) -> str:
    """
    Format committee analysis into a readable text block.

    Args:
        pass5: The pass 5 committee analysis JSON data.

    Returns:
        A formatted string describing committee structure.
    """
    lines = [
        f"Total legislative bodies: {pass5.get('total_bodies', 0)}",
        f"Active bodies (met in period): {pass5.get('active_bodies', 0)}",
        "",
    ]

    for comm in pass5.get("committees", []):
        lines.append(f"COMMITTEE: {comm['name']}")
        lines.append(f"  Role: {comm['role']}")
        lines.append(f"  Meetings (6 months): {comm['meeting_count']}")
        lines.append(f"  Recent focus: {comm['recent_focus']}")

        if comm.get("key_topics"):
            lines.append(f"  Key topics: {', '.join(comm['key_topics'])}")

        if comm.get("upcoming_issues"):
            lines.append("  Upcoming issues:")
            for issue in comm["upcoming_issues"]:
                lines.append(f"    - {issue}")

        lines.append("")

    if pass5.get("structural_observations"):
        lines.append("STRUCTURAL OBSERVATIONS:")
        for obs in pass5["structural_observations"]:
            lines.append(f"  - {obs}")

    return "\n".join(lines)


def format_document_summaries(pass4: dict, registry: dict | None = None) -> str:
    """
    Format document analysis into a readable text block.

    We group summaries by matter type for the LLM so it can
    organize the Key Documents section logically.

    If a source registry is provided, each document entry includes its
    Legistar source URL — so the LLM can add inline citations.

    Args:
        pass4: The pass 4 document analysis JSON data.
        registry: Optional source registry from build_source_registry().
                  When provided, each document gets a "Source URL:" line.

    Returns:
        A formatted string with document summaries grouped by type.
    """
    # Get the title→URL lookup (empty dict if no registry provided).
    title_to_url = registry["title_to_url"] if registry else {}

    # Group summaries by matter type.
    # This is a common Python pattern: build a dict of lists.
    by_type = {}
    for doc in pass4.get("summaries", []):
        matter_type = doc.get("matter_type", "Other")
        if matter_type not in by_type:
            by_type[matter_type] = []
        by_type[matter_type].append(doc)

    lines = [f"Documents analyzed: {pass4.get('documents_analyzed', 0)}", ""]

    for matter_type, docs in sorted(by_type.items()):
        lines.append(f"--- {matter_type} ({len(docs)} documents) ---")
        for doc in docs:
            lines.append(f"  Title: {doc['matter_title']}")

            # Try to find the source URL for this document's matter title.
            source_url = fuzzy_match_title(doc["matter_title"], title_to_url)
            if source_url:
                lines.append(f"  Source URL: {source_url}")

            lines.append(f"  Summary: {doc['summary']}")
            lines.append(f"  Key issue: {doc['key_issue']}")
            if doc.get("fiscal_impact") and doc["fiscal_impact"] != "Not explicitly mentioned":
                lines.append(f"  Fiscal impact: {doc['fiscal_impact']}")
            if doc.get("recommendation") and "does not contain" not in doc["recommendation"].lower():
                lines.append(f"  Recommendation: {doc['recommendation']}")
            lines.append("")

    if pass4.get("cross_cutting_themes"):
        lines.append("CROSS-CUTTING THEMES:")
        for theme in pass4["cross_cutting_themes"]:
            lines.append(f"  - {theme}")

    return "\n".join(lines)


def format_synthesis(pass6: dict) -> str:
    """
    Format synthesis data into a readable text block.

    Args:
        pass6: The pass 6 synthesis JSON data.

    Returns:
        A formatted string with themes, priorities, and advice.
    """
    lines = [
        "EXECUTIVE SUMMARY:",
        pass6.get("executive_summary", "No summary available."),
        "",
        "KEY THEMES:",
    ]

    for theme in pass6.get("key_themes", []):
        lines.append(f"  Theme: {theme['title']}")
        lines.append(f"  Description: {theme['description']}")
        lines.append(f"  Relevance: {theme['relevance']}")
        if theme.get("evidence"):
            lines.append("  Evidence:")
            for ev in theme["evidence"]:
                lines.append(f"    - {ev}")
        lines.append("")

    lines.append("IMMEDIATE PRIORITIES (First 90 Days):")
    for priority in pass6.get("immediate_priorities", []):
        lines.append(f"  - {priority}")

    lines.append("")
    lines.append("ONGOING ISSUES:")
    for issue in pass6.get("ongoing_issues", []):
        lines.append(f"  - {issue}")

    lines.append("")
    lines.append("KEY RELATIONSHIPS:")
    for rel in pass6.get("relationships_to_build", []):
        lines.append(f"  - {rel}")

    lines.append("")
    lines.append("KNOWLEDGE GAPS:")
    for gap in pass6.get("knowledge_gaps", []):
        lines.append(f"  - {gap}")

    return "\n".join(lines)


def format_constituent_data(demographics: dict, issue_scores: dict) -> str:
    """
    Format constituent demographic and issue priority data into a readable
    text block for the LLM.

    This covers CPO Steps 1 and 2: "Who are your constituents?" and
    "What do they care about?"

    Args:
        demographics: The demographics JSON from script 06.
        issue_scores: The issue_scores JSON from script 06.

    Returns:
        A formatted string with demographics, party breakdown, and ranked issues.
    """
    lines = [
        "VOTER DEMOGRAPHICS:",
        f"  Total registered voters: {demographics['total_voters']:,}",
        f"  Average age: {demographics['avg_age']}",
        "",
        "  Party registration:",
    ]

    party = demographics.get("party_breakdown", {})
    total = demographics["total_voters"]
    for label, count in party.items():
        pct = round((count / total) * 100, 1) if total else 0
        display = label.replace("_", " ").title()
        lines.append(f"    {display}: {count:,} ({pct}%)")

    gender = demographics.get("gender", {})
    lines.append("")
    lines.append("  Gender:")
    for label, count in gender.items():
        pct = round((count / total) * 100, 1) if total else 0
        display = label.replace("_", " ").title()
        lines.append(f"    {display}: {count:,} ({pct}%)")

    # Ideological context
    context = issue_scores.get("context_scores", {})
    if context:
        lines.append("")
        lines.append("  Ideological leaning (Haystaq predictive scores):")
        for label, score in context.items():
            lines.append(f"    {label}: {score}/100")

    # Issue priorities ranked by score
    lines.append("")
    lines.append(f"CONSTITUENT ISSUE PRIORITIES (based on {issue_scores.get('voter_count_with_scores', 0):,} voters with Haystaq scores):")
    lines.append("  Tier thresholds: 75+ = Critical, 60-74 = Strong, 50-59 = Moderate, <50 = Lower")
    lines.append("")

    for issue in issue_scores.get("issues", []):
        tier_label = issue.get("tier_label", "")
        lines.append(f"  {issue['name']}: {issue['score']}/100 [{tier_label}]")

    return "\n".join(lines)


def format_mismatch_data(mismatch: dict) -> str:
    """
    Format the council-vs-constituent mismatch analysis into a readable
    text block for the LLM.

    This is CPO Step 3: "Where does the council spend its time vs.
    what constituents care about?"

    Args:
        mismatch: The council_vs_constituent JSON from script 07.

    Returns:
        A formatted string with mismatch table and gap analysis.
    """
    lines = [
        f"Total council matters analyzed: {mismatch.get('total_council_matters', 0)}",
        f"Total voters with priority scores: {mismatch.get('total_voter_count', 0):,}",
        "",
        "ALIGNMENT TABLE (council agenda share vs. constituent priority score):",
        "",
    ]

    for item in mismatch.get("mismatch_table", []):
        score_str = f"{item['constituent_score']}/100" if item["constituent_score"] else "N/A"
        rank_str = f"Rank #{item['constituent_rank']}" if item["constituent_rank"] else ""
        lines.append(
            f"  {item['council_topic']}: "
            f"Council {item['council_pct']}% of agenda ({item['council_matters']} matters) | "
            f"Voter Score {score_str} {rank_str} → {item['gap_type']}"
        )

    # Highlight the gaps
    summary = mismatch.get("summary", {})
    under = summary.get("under_represented", [])
    if under:
        lines.append("")
        lines.append("UNDER-REPRESENTED ISSUES (voters care, council spends little time):")
        for item in under:
            lines.append(f"  ** {item['council_topic']}: {item['gap_description']}")
            if item.get("constituent_issues"):
                lines.append(f"     Voter dimensions: {', '.join(item['constituent_issues'])}")

    over = summary.get("over_represented", [])
    if over:
        lines.append("")
        lines.append("OVER-REPRESENTED ISSUES (council spends lots of time, voters less concerned):")
        for item in over:
            lines.append(f"  ** {item['council_topic']}: {item['gap_description']}")

    aligned = summary.get("aligned", [])
    if aligned:
        lines.append("")
        lines.append("ALIGNED ISSUES (council attention roughly matches voter priority):")
        for item in aligned:
            lines.append(f"  - {item['council_topic']}: {item['gap_description']}")

    unmapped = summary.get("unmapped", [])
    if unmapped:
        lines.append("")
        lines.append("UNMAPPED (administrative/operational — no direct voter equivalent):")
        for item in unmapped:
            lines.append(f"  - {item['council_topic']} ({item['council_pct']}% of agenda)")

    return "\n".join(lines)


def format_quick_wins(quick_wins: dict) -> str:
    """
    Format quick win recommendations into a readable text block for the LLM.

    This is CPO Step 5: "Quick wins with CTAs for polling — what can you do
    right now?" Each quick win is tied to a specific constituent gap and
    references concrete matters, committees, and data.

    Args:
        quick_wins: The quick_wins JSON from script 09.

    Returns:
        A formatted string with structured quick win recommendations.
    """
    wins = quick_wins.get("quick_wins", [])
    if not wins:
        return ""

    lines = [
        f"Total gap areas: {quick_wins.get('total_gap_areas', 0)}",
        f"Total recommended actions: {quick_wins.get('total_recommended_actions', 0)}",
        "",
    ]

    for gap in wins:
        lines.append(f"GAP AREA: {gap['gap_topic']}")
        lines.append(f"  Voter priority score: {gap['voter_score']}/100")
        lines.append(f"  Council agenda share: {gap['council_pct']}%")
        lines.append(f"  Voter issues: {', '.join(gap.get('voter_issues', []))}")
        if gap.get("committee"):
            lines.append(f"  Relevant committee: {gap['committee']}")
        lines.append(f"  Key matters: {'; '.join(gap.get('key_matters', []))}")
        if gap.get("contentious_matters"):
            lines.append(f"  Contentious matters: {'; '.join(gap['contentious_matters'])}")
        lines.append("")

        lines.append("  RECOMMENDED ACTIONS:")
        for action in gap.get("recommended_actions", []):
            lines.append(f"    [{action['priority'].upper()}] {action['title']}")
            lines.append(f"      Rationale: {action['rationale']}")
            if action.get("legistar_url"):
                lines.append(f"      Legistar URL: {action['legistar_url']}")
            if action.get("matter_status"):
                lines.append(f"      Status: {action['matter_status']}")
            if action.get("dissent_matters"):
                lines.append(f"      Related dissent: {'; '.join(action['dissent_matters'][:3])}")
            lines.append("")

    return "\n".join(lines)


def format_discussion_narratives(discussions: dict) -> str:
    """
    Format discussion narratives as a readable text block for the LLM prompt.

    Only includes narratives with high or medium confidence. Each narrative
    includes source citations so the LLM can produce inline links.

    Args:
        discussions: The discussion_narratives JSON from script 08.

    Returns:
        A formatted string with discussion context and source attributions.
    """
    narratives = discussions.get("narratives", [])

    # Filter to only high and medium confidence items.
    usable = [n for n in narratives if n.get("confidence") in ("high", "medium")]

    if not usable:
        return ""

    lines = [
        f"Total items researched: {discussions.get('items_researched', 0)}",
        f"Items with coverage: {discussions.get('items_with_narratives', 0)}",
        f"Sources verified: {discussions.get('total_sources_verified', 0)}",
        "",
        "IMPORTANT: This discussion context is sourced from local news coverage,",
        "not official meeting minutes. Use 'According to [Source]...' language.",
        "Only use the source URLs provided — do not fabricate links.",
        "",
    ]

    # Sort by confidence: high first, then medium.
    usable.sort(key=lambda n: 0 if n.get("confidence") == "high" else 1)

    for n in usable:
        title = n.get("item_title", "Unknown")
        lines.append(f"ITEM: {title}")
        lines.append(f"  Confidence: {n.get('confidence', 'unknown')}")
        if n.get("legistar_url"):
            lines.append(f"  Official record: {n['legistar_url']}")
        if n.get("meeting_date"):
            lines.append(f"  Meeting date: {n['meeting_date']}")
        if n.get("outcome"):
            lines.append(f"  Outcome: {n['outcome']}")
        if n.get("narrative_summary"):
            lines.append(f"  Summary: {n['narrative_summary']}")

        if n.get("key_arguments_for"):
            lines.append("  Arguments FOR:")
            for arg in n["key_arguments_for"]:
                lines.append(f"    - {arg}")

        if n.get("key_arguments_against"):
            lines.append("  Arguments AGAINST:")
            for arg in n["key_arguments_against"]:
                lines.append(f"    - {arg}")

        if n.get("council_positions"):
            lines.append("  Council member positions:")
            for pos in n["council_positions"]:
                name = pos.get("name", "Unknown")
                position = pos.get("position", "unknown")
                summary = pos.get("summary", "")
                sourced = pos.get("is_directly_sourced", False)
                tag = "[directly sourced]" if sourced else "[inferred]"
                lines.append(f"    - {name}: {position} {tag} — {summary}")
                if pos.get("direct_quote"):
                    lines.append(f"      Quote: \"{pos['direct_quote']}\"")

        if n.get("public_sentiment"):
            lines.append(f"  Public sentiment: {n['public_sentiment']}")

        # Source citations for the LLM to use as inline links.
        if n.get("sources"):
            lines.append("  News sources (use for inline citations):")
            for s in n["sources"]:
                if s.get("verified"):
                    lines.append(f"    {s['title']} | {s['url']} | {s.get('publication', 'Unknown')}")

        lines.append("")

    return "\n".join(lines)


def format_legislative_overview(pass1: dict) -> str:
    """
    Format legislative overview into a readable text block.

    Args:
        pass1: The pass 1 legislative overview JSON data.

    Returns:
        A formatted string summarizing the legislative landscape.
    """
    lines = [
        f"Time period: {pass1.get('time_period', 'Unknown')}",
        f"Total matters: {pass1.get('total_matters', 0)}",
        "",
        "TOPIC AREAS:",
    ]

    for topic in pass1.get("topic_areas", []):
        lines.append(f"  {topic['name']} ({topic['matter_count']} matters)")
        lines.append(f"    {topic['description']}")
        lines.append(f"    Significance: {topic['significance']}")
        if topic.get("key_matters"):
            lines.append("    Key matters:")
            for m in topic["key_matters"][:5]:
                lines.append(f"      - {m}")
        lines.append("")

    lines.append("NOTABLE PATTERNS:")
    for pattern in pass1.get("notable_patterns", []):
        lines.append(f"  - {pattern}")

    lines.append("")
    lines.append("TOP PRIORITIES:")
    for priority in pass1.get("top_priorities", []):
        lines.append(f"  - {priority}")

    return "\n".join(lines)


# ============================================================================
# MAIN ASSEMBLY
# ============================================================================

def assemble_briefing():
    """
    Load all analysis results and use the LLM to write a polished briefing.

    The approach:
    1. Load all 6 pass results from JSON files
    2. Format each into readable text blocks
    3. Send everything to the LLM with detailed instructions for the final document
    4. Save the LLM's markdown output as the briefing
    5. Save metadata (cost, timestamps, token counts) alongside it
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Check for API key.
    if not os.getenv("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found in environment.")
        print("Add it to your .env file: GEMINI_API_KEY=your_key_here")
        sys.exit(1)

    print("=" * 60)
    print(f"{cfg.city_name} POC — Briefing Assembly")
    print("=" * 60)
    print()

    # ------------------------------------------------------------------
    # 1. Load all analysis results
    # ------------------------------------------------------------------
    print("Loading analysis results...")

    pass1 = load_json(ANALYSIS_DIR / "pass1_legislative_overview.json")
    pass2 = load_json(ANALYSIS_DIR / "pass2_vote_analysis.json")
    pass3 = load_json(ANALYSIS_DIR / "pass3_budget_analysis.json")
    pass4 = load_json(ANALYSIS_DIR / "pass4_document_summaries.json")
    pass5 = load_json(ANALYSIS_DIR / "pass5_committee_analysis.json")
    pass6 = load_json(ANALYSIS_DIR / "pass6_synthesis.json")

    # Load raw budget data for accurate record counts in metadata.
    fiscal_data = load_json(DATA_DIR / "budget" / "government_fiscal.json")
    tax_data = load_json(DATA_DIR / "budget" / "property_tax_rate.json")

    # Check that all results are available.
    missing = []
    for name, data in [("pass1", pass1), ("pass2", pass2), ("pass3", pass3),
                        ("pass4", pass4), ("pass5", pass5), ("pass6", pass6)]:
        if data is None:
            missing.append(name)

    if missing:
        print(f"ERROR: Missing analysis results: {', '.join(missing)}")
        print("Run 04_run_analysis.py first.")
        sys.exit(1)

    print(f"  All 6 analysis passes loaded successfully")

    # Load constituent data (from script 06) and mismatch analysis (from script 07).
    # These are optional — the briefing can still be generated without them,
    # but the constituent sections will be skipped.
    demographics = load_json(CONSTITUENT_DIR / "demographics.json")
    issue_scores = load_json(CONSTITUENT_DIR / "issue_scores.json")
    mismatch = load_json(ANALYSIS_DIR / "council_vs_constituent.json")

    has_constituent_data = demographics is not None and issue_scores is not None
    has_mismatch_data = mismatch is not None

    if has_constituent_data:
        print(f"  Constituent data: {demographics['total_voters']:,} voters, {len(issue_scores.get('issues', []))} issues")
    else:
        print("  WARNING: Constituent data not found — run 06_collect_constituent_data.py")

    if has_mismatch_data:
        under = len(mismatch.get("summary", {}).get("under_represented", []))
        print(f"  Council vs. constituent mismatch: {under} under-represented gaps found")
    else:
        print("  WARNING: Mismatch data not found — run 07_council_vs_constituent.py")

    # Load discussion narratives (from script 08).
    # Optional — briefing still generates without them.
    discussions = load_json(DISCUSSION_DIR / "discussion_narratives.json")
    has_discussion_data = (
        discussions is not None
        and len(discussions.get("narratives", [])) > 0
        and discussions.get("items_with_narratives", 0) > 0
    )

    if has_discussion_data:
        n_items = discussions["items_with_narratives"]
        n_sources = discussions.get("total_sources_verified", 0)
        print(f"  Discussion narratives: {n_items} items with coverage, {n_sources} verified sources")
    else:
        print("  WARNING: Discussion narratives not found — run 08_collect_discussions.py")

    # Load quick wins (from script 09).
    # Optional — briefing still generates without them, but the "Quick Wins"
    # section will be skipped.
    quick_wins_data = load_json(ANALYSIS_DIR / "quick_wins.json")
    has_quick_wins = (
        quick_wins_data is not None
        and len(quick_wins_data.get("quick_wins", [])) > 0
    )

    if has_quick_wins:
        n_gaps = quick_wins_data["total_gap_areas"]
        n_actions = quick_wins_data["total_recommended_actions"]
        print(f"  Quick wins: {n_gaps} gap areas, {n_actions} recommended actions")
    else:
        print("  WARNING: Quick wins not found — run 09_generate_quick_wins.py")

    print()

    # ------------------------------------------------------------------
    # 1b. Build source registry for citations
    # ------------------------------------------------------------------
    print("Building source citation registry...")
    registry = build_source_registry()

    # Collect only the titles actually referenced in the analysis passes.
    # This keeps the reference table compact (~100-150 entries vs. 686).
    referenced = collect_referenced_titles(pass1, pass2, pass4, pass5, pass6)
    print(f"  Titles referenced in analysis: {len(referenced)}")

    source_ref_table = format_source_reference_table(registry, referenced, discussions if has_discussion_data else None)
    print(f"  Reference table: {len(source_ref_table):,} characters")
    print()

    # ------------------------------------------------------------------
    # 2. Format data for the LLM prompt
    # ------------------------------------------------------------------
    print("Formatting data for LLM...")

    legislative_text = format_legislative_overview(pass1)
    vote_text = format_vote_summary(pass2)
    budget_text = format_budget_summary(pass3)
    # Pass the registry so document summaries include source URLs.
    document_text = format_document_summaries(pass4, registry=registry)
    committee_text = format_committee_summary(pass5)
    synthesis_text = format_synthesis(pass6)

    # Format constituent data (if available).
    constituent_text = ""
    if has_constituent_data:
        constituent_text = format_constituent_data(demographics, issue_scores)
    mismatch_text = ""
    if has_mismatch_data:
        mismatch_text = format_mismatch_data(mismatch)
    discussion_text = ""
    if has_discussion_data:
        discussion_text = format_discussion_narratives(discussions)
    quick_wins_text = ""
    if has_quick_wins:
        quick_wins_text = format_quick_wins(quick_wins_data)

    # Today's date for the briefing header.
    # date.today() returns the current date as a date object.
    # .strftime() formats it as a string — %B = full month name, %d = day, %Y = year.
    today = date.today().strftime("%B %d, %Y")

    # ------------------------------------------------------------------
    # 3. Build the LLM prompt
    # ------------------------------------------------------------------
    # This is a long prompt, but it needs to be — we're asking the LLM to write
    # a multi-section document with specific formatting requirements.

    # Build optional data sections for the prompt.
    # These are injected into the research data block only if the data is available.
    constituent_data_block = ""
    if constituent_text:
        voter_count = demographics["total_voters"] if demographics else 0
        voter_label = f"{voter_count:,}+" if voter_count else ""
        constituent_data_block = (
            f"\n--- CONSTITUENT DATA (from Haystaq voter modeling, {voter_label} voters) ---\n"
            + constituent_text
            + "\n"
        )

    mismatch_data_block = ""
    if mismatch_text:
        mismatch_data_block = (
            "\n--- COUNCIL vs. CONSTITUENT PRIORITY ALIGNMENT ---\n"
            + mismatch_text
            + "\n"
        )

    discussion_data_block = ""
    if discussion_text:
        discussion_data_block = (
            "\n--- DISCUSSION NARRATIVES (from local news coverage — use with attribution) ---\n"
            + discussion_text
            + "\n"
        )

    quick_wins_data_block = ""
    if quick_wins_text:
        quick_wins_data_block = (
            "\n--- QUICK WINS (concrete actions tied to constituent priority gaps) ---\n"
            + quick_wins_text
            + "\n"
        )

    # Build optional section instructions.
    # Section numbers are dynamic based on what data is available.
    constituent_section_instruction = ""
    if has_constituent_data:
        constituent_section_instruction = """
4. **Your Constituents** — A data-driven profile of {cfg.city_name}'s registered voters.
   Include a compact summary table with: total voters, party breakdown (with percentages),
   average age, gender split, and ideological leaning. Then present the top constituent
   issue priorities as a ranked table: Issue Name | Score (out of 100) | Tier (Strong/
   Moderate/Lower). Use the tier labels from the data. Explain what these scores mean —
   they are predictive scores from Haystaq voter modeling,
   indicating the percentage of voters in {cfg.city_name} likely to prioritize each issue.
   Write 1-2 paragraphs interpreting the pattern: what do {cfg.city_name} voters care most
   about? What might surprise a new council member?
"""

    mismatch_section_instruction = ""
    if has_mismatch_data:
        mismatch_section_instruction = """
5. **Council vs. Constituent Priorities** — This is the key strategic insight.
   Present a markdown table comparing each council topic area's share of the
   legislative agenda (% of matters) with the corresponding constituent priority
   score. Columns: Topic | Council % | Voter Score | Gap Type.
   After the table, write 2-3 paragraphs analyzing:
   - UNDER-REPRESENTED gaps: issues voters rank highly but the council barely
     addresses (these are political opportunities for a new council member)
   - OVER-REPRESENTED areas: topics consuming disproportionate council time
     relative to voter concern (often administrative/operational)
   - What this means for the council member's agenda-setting strategy
   Be specific: name the topics, cite the numbers, and explain why each gap matters.
"""

    quick_wins_section_instruction = ""
    if has_quick_wins:
        quick_wins_section_instruction = """
5b. **Quick Wins: What You Can Do Right Now** — This section turns the constituent
    priority gaps into specific, actionable steps. For each under-represented gap
    area, present 3-5 concrete actions the council member can take in their first
    weeks. Use the quick wins data provided.

    For each gap area, structure as:
    ### [Topic Name]: Closing the Gap
    - Brief context: voter priority score vs. council agenda share
    - **HIGH priority actions** (committee engagement, staff briefings): present as
      bold action items with 1-2 sentences of rationale
    - **MEDIUM priority actions** (specific matters to study): present as a bulleted
      list with matter titles as inline markdown links to their Legistar URLs
    - **Context actions** (news coverage, past dissent): present as additional reading

    Make this section feel like a practical to-do list, not an analysis paper. Use
    imperative voice: "Join...", "Request...", "Review...", "Study...".
    Each action should explain WHY it matters for constituent alignment.
"""

    discussion_section_instruction = ""
    if has_discussion_data:
        discussion_section_instruction = """
9b. **Behind the Votes: Key Debates** — For the most contentious items, tell the
    story of what was discussed. Who argued for, who argued against, and why? What
    did the public say during hearings? This section transforms bare vote tallies
    into narratives that help the new member understand the political dynamics.

    CRITICAL RULES for this section:
    - Only include items where discussion content was found (confidence "high" or "medium").
    - ALWAYS attribute: "According to [Source Name](url), ..." or "News reports indicate..."
    - Never present news-sourced claims as bare facts.
    - For council member positions, use hedged language: "reportedly opposed",
      "expressed concerns about", "voted against" — unless you have a direct quote.
    - Only use source URLs from the discussion narrative data. Do NOT fabricate links.
    - Include this disclaimer at the start of the section:
      "*Discussion context is sourced from local news coverage, not official meeting
      minutes. Claims reflect journalistic reporting and may not capture the full
      nuance of council deliberations.*"
"""

    first_90_days_extra = ""
    if has_constituent_data:
        first_90_days_extra = (
            " Incorporate the constituent priority gaps — for example, if housing"
            " is under-represented, recommend the new member review specific"
            " housing-related matters and connect with housing stakeholders."
        )

    # Build dynamic data source stats from loaded analysis results.
    n_matters = pass1.get("total_matters", 0)
    n_docs = pass4.get("documents_analyzed", 0)
    n_voter_count = demographics["total_voters"] if demographics else 0
    n_issue_dims = len(issue_scores.get("issues", [])) if issue_scores else 0

    data_sources_note = f"Legistar API, {cfg.state_name} LINC/OSBM, PDF staff reports"
    appendix_note = (
        f"Mention: {n_matters} legislative matters, {n_docs} PDF attachments analyzed,\n"
        f"    6 LLM analysis passes."
    )
    if has_constituent_data:
        data_sources_note += ", Haystaq voter modeling"
        appendix_note = (
            f"Mention: {n_matters} legislative matters, {n_docs} PDF attachments analyzed,\n"
            f"    6 LLM analysis passes, Haystaq voter modeling for {n_voter_count:,}+\n"
            f"    voters across {n_issue_dims} issue dimensions."
        )
    if has_discussion_data:
        data_sources_note += ", local news coverage (web search)"
        appendix_note += (
            "\n    Discussion context: web-searched news coverage for contentious items, with\n"
            "    source URLs stored and verified. Discussion claims are attributed to sources\n"
            "    and flagged by confidence level."
        )

    prompt = f"""You are a professional policy analyst writing a briefing document for a newly
elected {cfg.city_name_full} {cfg.governing_body} member. You have comprehensive research data from
6 analysis passes covering legislation, votes, budgets, documents, committees, and synthesis.

Your job is to write a polished, well-organized MARKDOWN briefing document that this
{cfg.member_title.lower()} could read in 30-45 minutes and walk away with a solid understanding
of {cfg.city_name} {cfg.entity_type} government.

TODAY'S DATE: {today}
DATA PERIOD: {cfg.data_period_display}

=== RESEARCH DATA ===

--- LEGISLATIVE OVERVIEW ---
{legislative_text}

--- VOTING & DECISION PATTERNS ---
{vote_text}

--- BUDGET & FISCAL CONTEXT ---
{budget_text}

--- KEY DOCUMENTS ---
{document_text}

--- COMMITTEE STRUCTURE ---
{committee_text}

--- SYNTHESIS & RECOMMENDATIONS ---
{synthesis_text}
{constituent_data_block}{mismatch_data_block}{quick_wins_data_block}{discussion_data_block}
=== END OF RESEARCH DATA ===

{source_ref_table}

WRITING INSTRUCTIONS:

Write the briefing as a COMPLETE MARKDOWN DOCUMENT with the following sections.
Use proper markdown headers (##, ###), bullet points, bold text, and tables where
appropriate. Write in clear, direct prose — not bureaucratic jargon.

REQUIRED SECTIONS (in this order):

1. **Title and Header** — "{cfg.city_name} {cfg.governing_body} — New Member Briefing" with the
   date and data period. Include a brief note about data sources ({data_sources_note}).

2. **Executive Summary** (3-5 paragraphs) — The most important things to know,
   written so a busy person can read just this section and understand the state of
   {cfg.city_name} city government. Include the biggest themes, the fiscal picture,
   the council's recent focus, AND the key constituent priority gaps. Use specific numbers.

3. **Key Themes** — The 5-7 most important themes from the synthesis, each with
   a clear title, 2-3 sentence description, and bullet points of supporting evidence.
{constituent_section_instruction}{mismatch_section_instruction}{quick_wins_section_instruction}
6. **Your First 90 Days** — A concrete, actionable checklist of what to do in the
   first 90 days. Include specific meetings to attend, documents to read, people
   to meet, and topics to study. Be very specific — name names and reference actual
   matter titles.{first_90_days_extra}

7. **Legislative Landscape** — Overview of what the council has been working on.
   Include a markdown table of topic areas with matter counts and a SHORT (under
   15 words) description per row. Put detailed discussion in prose paragraphs
   AFTER the table, not inside table cells. Describe the mix of routine consent
   items vs. substantive policy decisions.

8. **Budget & Fiscal Context** — {cfg.city_name}'s financial picture. Include a table of
   key fiscal metrics (latest revenue, expenditure, growth rates). Discuss revenue
   trends, spending trends, tax rate history, and debt. Highlight both strengths
   and concerns.

9. **How the Council Votes** — Voting patterns, who's most active, what's
   contentious vs. unanimous. Include a compact table of council member motion
   activity (name, motions made, motions seconded — no long descriptions in cells).
   Call out the specific items that generated dissent — these are the politically
   interesting topics.
{discussion_section_instruction}
10. **Committee Guide** — A practical guide to each active committee: what it does,
    how often it meets, and what it's been focused on. Present as a compact table
    (name, meetings, short focus area — keep each cell under 20 words) followed by
    prose paragraphs with more detail. Explain how items flow from committee to
    full council.

11. **Key Staff Reports & Documents** — Summaries of the 10-15 most important
    documents, grouped by topic. For each, include the title, a 1-2 sentence summary,
    and the key decision or fiscal impact.

12. **Ongoing Issues to Watch** — Issues that will continue to require attention
    over the full council term. Be specific about what to look for and why it matters.

13. **Key Relationships to Build** — Specific people, organizations, and stakeholder
    groups the council member should connect with, and why.

14. **Appendix: Data Sources & Methodology** — Brief note explaining where the data
    came from, how many documents were analyzed, and the cost of the analysis.
    {appendix_note}

FORMATTING RULES:
- Use ## for main sections, ### for subsections
- Use markdown tables (|---|---|) for data comparisons
- IMPORTANT: Keep every table cell under 20 words. Put detailed analysis in prose
  paragraphs AFTER the table, not crammed into table cells. Tables are for quick
  scanning; prose is for depth.
- Use **bold** for key terms and numbers
- Use bullet points for lists
- Keep paragraphs to 3-5 sentences
- Write for someone smart but new to city government
- Use actual dollar amounts, percentages, and names from the data
- Do NOT use placeholder text — every statement should be grounded in the data above
- The document should be 4000-6000 words

SOURCE CITATION RULES:
- A Source Reference Table is provided above with matter titles and their URLs.
- When you first mention a specific legislative matter or document in a section,
  make it a markdown link: [Matter Title](URL). Only link the FIRST mention per section.
- For budget/fiscal claims (dollar amounts, growth rates, tax rates), link to the
  appropriate LINC dataset on first mention per section:
  [LINC Government Fiscal Data](budget_url) or [LINC Property Tax Data](tax_url).
- ONLY use URLs from the Source Reference Table. Do NOT invent or guess URLs.
- If you mention a matter that is NOT in the reference table, just use plain text
  (no link) — do not fabricate a URL.
- Aim for 15-30 inline citations across the entire document. Focus on the most
  important and verifiable claims.
- In the Appendix section, mention that the briefing includes inline source links
  to {cfg.city_name}'s Legistar portal and {cfg.state_name} LINC/OSBM fiscal datasets."""

    # ------------------------------------------------------------------
    # 4. Call the LLM
    # ------------------------------------------------------------------
    print(f"Sending to LLM ({len(prompt):,} chars)...")
    start_time = time.time()

    client = GeminiClient(
        default_model=MODEL,
        default_temperature=TEMPERATURE,
        # No thinking for this pass — we're generating prose, not reasoning.
        # Thinking can inflate the response with internal reasoning text.
        thinking_budget=0,
        max_connections=10,
        max_keepalive_connections=5,
        max_retries=3,
    )

    # generate_content() returns plain text (markdown in this case).
    # We use this instead of generate_structured_content() because the output
    # is a free-form document, not structured JSON.
    # max_tokens caps the output length — 32K tokens ≈ 24K words.
    # Increased from 16K because discussion narrative URLs (Gemini grounding
    # redirects) are very long and consume more tokens than plain text.
    briefing_text = client.generate_content(
        prompt=prompt,
        temperature=TEMPERATURE,
        max_tokens=32000,
        trace_name="assemble_briefing",
    )

    elapsed = time.time() - start_time
    stats = client.get_usage_stats()

    print(f"  LLM response received in {elapsed:.1f}s")
    print(f"  Output: {len(briefing_text):,} characters")
    print(f"  Cost: ${stats['total_cost']:.4f}")
    print()

    # ------------------------------------------------------------------
    # 5. Save the briefing
    # ------------------------------------------------------------------
    print("Saving briefing...")

    # Save the markdown briefing.
    briefing_path = OUTPUT_DIR / f"{cfg.city_name.lower().replace(' ', '_')}_{cfg.governing_body_short}_briefing.md"
    with open(briefing_path, "w", encoding="utf-8") as f:
        f.write(briefing_text)

    print(f"  Saved: {briefing_path.name}")

    # Save metadata about the briefing generation.
    # This is useful for tracking costs and reproducing results.
    metadata = {
        "generated_date": today,
        "data_period": cfg.data_period,
        "municipality": cfg.city_name_full,
        "model": MODEL.value,
        "temperature": TEMPERATURE,
        "prompt_chars": len(prompt),
        "output_chars": len(briefing_text),
        "generation_time_seconds": round(elapsed, 1),
        "api_calls": stats["api_call_count"],
        "prompt_tokens": stats["total_prompt_tokens"],
        "completion_tokens": stats["total_completion_tokens"],
        "total_cost": round(stats["total_cost"], 6),
        "data_sources": {
            "legislative_matters": pass1.get("total_matters", 0),
            "agenda_items_with_votes": pass2.get("total_items_with_actions", 0),
            "fiscal_records": len(fiscal_data) if fiscal_data else 0,
            "tax_rate_records": len(tax_data) if tax_data else 0,
            "documents_analyzed": pass4.get("documents_analyzed", 0),
            "committees_profiled": pass5.get("active_bodies", 0),
            "registered_voters": demographics["total_voters"] if demographics else 0,
            "voters_with_haystaq_scores": issue_scores.get("voter_count_with_scores", 0) if issue_scores else 0,
            "constituent_issue_dimensions": len(issue_scores.get("issues", [])) if issue_scores else 0,
            "council_constituent_gaps": len(mismatch.get("summary", {}).get("under_represented", [])) if mismatch else 0,
            "discussion_narratives": discussions.get("items_with_narratives", 0) if discussions else 0,
            "discussion_sources_verified": discussions.get("total_sources_verified", 0) if discussions else 0,
            "quick_win_gap_areas": quick_wins_data.get("total_gap_areas", 0) if quick_wins_data else 0,
            "quick_win_actions": quick_wins_data.get("total_recommended_actions", 0) if quick_wins_data else 0,
        },
        "analysis_passes": [
            "pass1_legislative_overview",
            "pass2_vote_analysis",
            "pass3_budget_analysis",
            "pass4_document_summaries",
            "pass5_committee_analysis",
            "pass6_synthesis",
            "constituent_demographics",
            "constituent_issue_scores",
            "council_vs_constituent_mismatch",
            "discussion_narratives",
            "quick_wins",
        ],
        "citation_sources": {
            "legistar_matter_urls": registry["matter_count"],
            "document_urls": len(registry["filename_to_url"]),
            "budget_urls": len(registry["budget_urls"]),
            "reference_table_chars": len(source_ref_table),
        },
    }

    metadata_path = OUTPUT_DIR / "briefing_metadata.json"
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)

    print(f"  Saved: {metadata_path.name}")
    print()

    # ------------------------------------------------------------------
    # SUMMARY
    # ------------------------------------------------------------------
    print("=" * 60)
    print("Briefing Assembly Complete!")
    print("=" * 60)
    print(f"  Output:     {briefing_path.resolve()}")
    print(f"  Length:     {len(briefing_text):,} characters (~{len(briefing_text) // 5:,} words)")
    print(f"  LLM time:  {elapsed:.1f}s")
    print(f"  LLM cost:  ${stats['total_cost']:.4f}")
    print(f"  Tokens:    {stats['total_prompt_tokens']:,} prompt + {stats['total_completion_tokens']:,} completion")
    print("=" * 60)


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    assemble_briefing()
