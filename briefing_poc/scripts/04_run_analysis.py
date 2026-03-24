"""
04_run_analysis.py — Run multi-pass LLM analysis on Charlotte data.

This script takes all the data collected by scripts 01-03 (Legistar legislation,
budget/fiscal data, and extracted PDF text) and runs it through 6 sequential
LLM analysis passes. Each pass focuses on a different aspect of the data:

  Pass 1: Legislative Overview — categorize 686 matters by topic area
  Pass 2: Key Decisions & Votes — parse action text, find voting patterns
  Pass 3: Budget & Fiscal Context — analyze 44 years of fiscal trends
  Pass 4: Key Documents Deep Dive — summarize the most important staff reports
  Pass 5: Committee Structure & Activity — what each committee works on
  Pass 6: Synthesis — cross-reference everything into themes and priorities

We use Gemini 2.5 Flash for cost efficiency:
  - Input:  $0.075 per million tokens
  - Output: $0.30 per million tokens
  - Estimated total cost for all 6 passes: ~$0.10-0.25

Each pass saves its result as a JSON file in data/analysis/. If you re-run the
script, it skips passes whose output files already exist (delete the file to
re-run a specific pass).

Usage:
    python scripts/04_run_analysis.py

Prerequisites:
    - GEMINI_API_KEY in your .env file
    - Data from scripts 01-03 in data/ directory

Output:
    data/analysis/pass1_legislative_overview.json
    data/analysis/pass2_vote_analysis.json
    data/analysis/pass3_budget_analysis.json
    data/analysis/pass4_document_summaries.json
    data/analysis/pass5_committee_analysis.json
    data/analysis/pass6_synthesis.json
"""

# --- Standard library imports ---
# json for reading/writing JSON files.
import json

# pathlib.Path for clean file path handling.
from pathlib import Path

# time for measuring how long each pass takes.
import time

# sys for command-line arguments and exit.
import sys

# os for environment variable access.
import os

# typing for type hints — helps document what types functions expect and return.
from typing import Optional

# --- Third-party imports ---
# Pydantic BaseModel for defining structured LLM response schemas.
# When we tell the LLM to respond as JSON matching a Pydantic model,
# it guarantees the response has the exact fields and types we defined.
from pydantic import BaseModel

# dotenv loads environment variables from a .env file.
# This is how we provide the GEMINI_API_KEY without hardcoding it.
from dotenv import load_dotenv

# --- Project imports ---
# GeminiClient is our wrapper around Google's Gemini API.
# It handles authentication, retries, cost tracking, and structured output.
# GeminiModelType is an enum that selects which Gemini model to use.
from shared.llm_gemini import GeminiClient, GeminiModelType
from city_config import cfg
from utils import load_json


# Load environment variables from .env file.
# This makes GEMINI_API_KEY available via os.getenv().
load_dotenv()


# ============================================================================
# CONFIGURATION
# ============================================================================

# Where data from scripts 01-03 lives.
DATA_DIR = cfg.data_dir

# Where to save analysis results.
OUTPUT_DIR = DATA_DIR / "analysis"

# Gemini 2.5 Flash is the best balance of speed, quality, and cost.
# It has a 1 million token context window — enough to send large amounts
# of data in a single request without chunking.
MODEL = GeminiModelType.FLASH

# Temperature controls randomness in the LLM's output.
# 0.0 = deterministic (same input → same output every time)
# 0.7 = moderate creativity
# 1.0 = maximum randomness
# For analysis tasks, we want low temperature for consistent, factual output.
TEMPERATURE = 0.2

# Thinking budget controls the model's internal reasoning.
# -1 = dynamic (model decides how much to think based on task complexity)
# 0 = no thinking (fastest, cheapest, fine for simple extraction)
# 1024+ = fixed budget (more thinking = better quality for complex tasks)
# We default to 0 (no thinking) for most passes, and use dynamic for synthesis.
DEFAULT_THINKING_BUDGET = 0

# How many of the most important documents to analyze in Pass 4.
# Each document gets its own LLM call, so more = more cost + time.
# 50 gives a good sample across different topic areas.
MAX_DOCUMENTS_TO_ANALYZE = 50


# ============================================================================
# PYDANTIC RESPONSE MODELS
# ============================================================================
# These models define the exact JSON structure the LLM must return.
# Gemini's "structured output" mode forces the response to match the schema.
# This eliminates the need to parse free-form text and handle format errors.
#
# Why Pydantic?
# - Python's most popular data validation library
# - Defines a class with typed fields → validates JSON automatically
# - Used across gp-ai-projects for all LLM structured output
# - Like TypeScript interfaces but with runtime validation


# --- Pass 1: Legislative Overview ---

class TopicArea(BaseModel):
    """A category of legislation identified by the LLM."""
    name: str                       # e.g., "Housing & Development"
    description: str                # What this topic covers
    matter_count: int               # How many matters fall in this category
    key_matters: list[str]          # Titles of the most notable matters
    significance: str               # Why this matters for a new council member
    # Optional: Legistar MatterIds for the key matters listed above.
    # These enable direct URL construction for source citations.
    # Default [] makes this backward-compatible with existing cached JSON.
    key_matter_ids: list[str] = []


class LegislativeOverview(BaseModel):
    """Pass 1 output: topics, patterns, and priorities across all legislation."""
    time_period: str                # e.g., "September 2025 - February 2026"
    total_matters: int              # Total count of matters analyzed
    topic_areas: list[TopicArea]    # Major topic categories found
    notable_patterns: list[str]     # Interesting patterns the LLM noticed
    top_priorities: list[str]       # What a new council member should focus on first


# --- Pass 2: Vote & Decision Analysis ---

class VotingPattern(BaseModel):
    """Analysis of how the council votes."""
    total_items_with_actions: int   # How many agenda items had action text
    unanimous_count: int            # Items passed unanimously
    non_unanimous_count: int        # Items with dissent or split votes
    deferred_count: int             # Items postponed for later
    denied_count: int               # Items voted down
    key_motions: list[str]          # Notable motion descriptions
    frequent_movers: list[str]      # Council members who frequently make motions
    dissent_items: list[str]        # Titles of items that weren't unanimous
    patterns: list[str]             # Observations about voting behavior


# --- Pass 3: Budget & Fiscal Analysis ---

class BudgetAnalysis(BaseModel):
    """Pass 3 output: fiscal trends and financial context."""
    summary: str                    # 2-3 paragraph overview
    total_revenue_latest: str       # Most recent year's total revenue
    total_expenditure_latest: str   # Most recent year's total spending
    revenue_trends: list[str]       # Key trends in revenue sources
    expenditure_trends: list[str]   # Key trends in spending categories
    tax_rate_analysis: str          # Property tax rate trends and context
    per_capita_analysis: str        # Revenue/spending per resident trends
    debt_analysis: str              # Debt service trends
    concerns: list[str]             # Fiscal concerns a new member should know
    strengths: list[str]            # Fiscal strengths of the city


# --- Pass 4: Document Summaries ---

class DocumentSummary(BaseModel):
    """Summary of a single staff report or attachment."""
    filename: str                   # Source file name
    matter_title: str               # Title from the linked legislation
    matter_type: str                # e.g., "Business Item", "Policy Item"
    summary: str                    # 2-3 sentence summary
    key_issue: str                  # The core issue or decision
    fiscal_impact: str              # Cost or budget impact (if mentioned)
    recommendation: str             # Staff recommendation (if mentioned)
    # Optional: Legistar MatterId parsed from the filename.
    # Enables direct URL construction for source citations.
    # Default "" makes this backward-compatible with existing cached JSON.
    matter_id: str = ""
    # Optional: Pre-built Legistar gateway URL for this document's matter.
    source_url: str = ""


class DocumentAnalysis(BaseModel):
    """Pass 4 output: summaries of key staff reports."""
    documents_analyzed: int         # How many documents were summarized
    summaries: list[DocumentSummary]
    cross_cutting_themes: list[str] # Themes that appear across multiple documents


# --- Pass 5: Committee Analysis ---

class CommitteeProfile(BaseModel):
    """Profile of a single committee/legislative body."""
    name: str                       # Committee name
    role: str                       # What this committee does
    meeting_count: int              # Meetings in the time period
    key_topics: list[str]           # Main topics they've worked on
    recent_focus: str               # What they've been focused on recently
    upcoming_issues: list[str]      # Issues likely to come up next


class CommitteeAnalysis(BaseModel):
    """Pass 5 output: committee structure and activity."""
    total_bodies: int               # Total legislative bodies
    active_bodies: int              # Bodies that met in the time period
    committees: list[CommitteeProfile]
    structural_observations: list[str]  # How the committee system works


# --- Pass 6: Synthesis ---

class KeyTheme(BaseModel):
    """A major theme identified across all analysis passes."""
    title: str                      # e.g., "Rapid Growth Management"
    description: str                # 2-3 sentence explanation
    evidence: list[str]             # What data supports this theme
    relevance: str                  # Why this matters for a new council member


class Synthesis(BaseModel):
    """Pass 6 output: cross-cutting themes and priorities."""
    executive_summary: str          # 3-5 paragraph high-level overview
    key_themes: list[KeyTheme]      # Major themes across all data
    immediate_priorities: list[str] # What to focus on in the first 90 days
    ongoing_issues: list[str]       # Long-running issues to track
    relationships_to_build: list[str]  # Key stakeholders and partnerships
    knowledge_gaps: list[str]       # Areas where more information is needed


# ============================================================================
# DATA LOADING HELPERS
# ============================================================================

def save_result(data: any, filename: str) -> Path:
    """
    Save an analysis result as a JSON file.

    If the data is a Pydantic model, we convert it to a dict first
    using .model_dump() (Pydantic v2's method for serialization).

    Args:
        data: The data to save (Pydantic model, dict, or list).
        filename: What to name the file (without extension).

    Returns:
        Path to the saved file.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    file_path = OUTPUT_DIR / f"{filename}.json"

    # isinstance() checks if an object is of a given type.
    # BaseModel is the parent class for all our Pydantic response models.
    if isinstance(data, BaseModel):
        # .model_dump() converts a Pydantic model to a plain Python dict.
        # This is the Pydantic v2 way — v1 used .dict() instead.
        serializable = data.model_dump()
    else:
        serializable = data

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(serializable, f, indent=2, ensure_ascii=False)

    return file_path


def pass_already_done(filename: str) -> bool:
    """Check if a pass's output file already exists (for resumable runs)."""
    return (OUTPUT_DIR / f"{filename}.json").exists()


# ============================================================================
# ANALYSIS PASSES
# ============================================================================

def run_pass_1(client: GeminiClient) -> LegislativeOverview:
    """
    Pass 1: Legislative Overview — Categorize all matters by topic.

    Input: 686 matters with titles, types, statuses, and body names.
    Task: Group into topic areas, identify patterns, suggest priorities.
    Estimated tokens: ~35K input, ~5K output ≈ $0.004
    """
    print("  Loading matters data...")
    matters = load_json(DATA_DIR / "legistar" / "matters.json")
    if not matters:
        raise FileNotFoundError("matters.json not found — run 01_collect_legistar.py first")

    # Build a condensed summary of each matter for the LLM.
    # We only send the fields the LLM needs — not the full API response.
    # This keeps the prompt smaller and cheaper.
    matter_lines = []
    for m in matters:
        # .get() returns a default value if the key doesn't exist.
        # This prevents KeyError exceptions on incomplete records.
        title = m.get("MatterTitle", "Untitled")
        matter_type = m.get("MatterTypeName", "Unknown")
        status = m.get("MatterStatusName", "Unknown")
        body = m.get("MatterBodyName", "Unknown")

        # f-strings (formatted string literals) embed variables directly.
        # They're Python's equivalent of template literals in JavaScript.
        matter_lines.append(f"- [{matter_type}] [{status}] [{body}] {title}")

    # "\n".join() concatenates all lines with newlines between them.
    # This is like Array.join("\n") in JavaScript.
    matters_text = "\n".join(matter_lines)

    prompt = f"""You are analyzing legislative data for {cfg.city_name_long} to prepare
a briefing for a newly elected {cfg.member_title.lower()}.

Below are {len(matters)} legislative matters from the past 6 months ({cfg.data_period}).
Each line shows: [Type] [Status] [Body] Title

MATTERS:
{matters_text}

TASK:
1. Identify the major TOPIC AREAS (e.g., Housing, Transportation, Budget, Zoning, Public Safety,
   Economic Development, Infrastructure, etc.). For each topic:
   - Name and describe the topic area
   - Count how many matters fall in this category
   - List the 3-5 most notable/important matter titles
   - Explain why this topic matters for a new council member

2. Identify NOTABLE PATTERNS across the legislation:
   - What types of legislation dominate?
   - Are there recurring themes or trends?
   - What's the ratio of routine (consent) vs. substantive items?

3. Suggest TOP PRIORITIES — what should a new {cfg.member_title.lower()} focus on understanding first?

Be specific and data-driven. Reference actual matter titles."""

    print(f"  Sending {len(matters)} matters to LLM ({len(prompt)} chars)...")

    # generate_structured_content() forces the LLM to return JSON matching
    # our Pydantic model. This is more reliable than asking for JSON in the prompt
    # and hoping the LLM formats it correctly.
    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=LegislativeOverview,
        temperature=TEMPERATURE,
        thinking_budget=DEFAULT_THINKING_BUDGET,
        trace_name="pass1_legislative_overview",
    )

    return result


def run_pass_2(client: GeminiClient) -> VotingPattern:
    """
    Pass 2: Key Decisions & Votes — Parse action text to find voting patterns.

    Input: Agenda items that have action text (motions, votes, outcomes).
    Task: Parse who made motions, who seconded, whether votes were unanimous.
    Estimated tokens: ~25K input, ~5K output ≈ $0.003
    """
    print("  Loading event items...")

    # event_items/ has one JSON file per meeting event. Each file contains
    # a list of agenda items with their action text (if any).
    event_items_dir = DATA_DIR / "legistar" / "event_items"

    if not event_items_dir.exists():
        raise FileNotFoundError("event_items/ not found — run 01_collect_legistar.py first")

    # Collect all agenda items that have action text.
    # Action text contains vote records like: "A motion was made by Council Member X
    # and seconded by Council Member Y to Approve this item. The motion carried unanimously."
    items_with_actions = []

    # .glob("*.json") finds all JSON files in the directory.
    # sorted() ensures deterministic ordering (same order every run).
    for json_file in sorted(event_items_dir.glob("*.json")):
        items = load_json(json_file)
        if not items:
            continue

        for item in items:
            action_text = item.get("EventItemActionText", "")
            if action_text and len(action_text.strip()) > 10:
                items_with_actions.append({
                    "title": item.get("EventItemTitle", "Untitled"),
                    "action": action_text.strip(),
                    # EventItemPassedFlagName tells us if the roll-call vote was used.
                    "roll_call": item.get("EventItemRollCallFlag", 0),
                })

    # Build the prompt with all action items.
    action_lines = []
    for i, item in enumerate(items_with_actions):
        action_lines.append(f"ITEM {i + 1}: {item['title']}\nACTION: {item['action']}\n")

    actions_text = "\n".join(action_lines)

    prompt = f"""You are analyzing voting and decision records from {cfg.city_name_full} {cfg.governing_body}
meetings ({cfg.data_period}) to prepare a briefing for a newly elected {cfg.member_title.lower()}.

Below are {len(items_with_actions)} agenda items that have recorded actions (motions, votes, decisions).

ACTION RECORDS:
{actions_text}

TASK:
1. Count the total items with actions, how many were unanimous, how many had dissent,
   how many were deferred, and how many were denied.

2. Identify which council members most frequently MAKE MOTIONS and SECOND motions.
   List them by name.

3. Identify any items that were NOT unanimous — these are the most politically interesting
   items. List their titles.

4. List 5-8 KEY MOTIONS that were particularly significant (large budget items,
   controversial topics, policy changes, etc.).

5. Describe PATTERNS you observe in how the council votes:
   - Do certain members tend to pair up on motions?
   - Are there items that needed multiple votes or were deferred repeatedly?
   - What percentage of business is routine consent vs. substantive debate?

Be specific. Name names. Reference actual items."""

    print(f"  Sending {len(items_with_actions)} action items to LLM ({len(prompt)} chars)...")

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=VotingPattern,
        temperature=TEMPERATURE,
        thinking_budget=DEFAULT_THINKING_BUDGET,
        trace_name="pass2_vote_analysis",
    )

    return result


def run_pass_3(client: GeminiClient) -> BudgetAnalysis:
    """
    Pass 3: Budget & Fiscal Context — Analyze Charlotte's fiscal trends.

    Input: 1,826 government fiscal records + 41 property tax rate records
           spanning 1980-2024 with 48 variables.
    Task: Identify revenue/expenditure trends, tax rate changes, fiscal health.
    Estimated tokens: ~50K input, ~5K output ≈ $0.005
    """
    print("  Loading budget data...")
    fiscal_data = load_json(DATA_DIR / "budget" / "government_fiscal.json")
    tax_data = load_json(DATA_DIR / "budget" / "property_tax_rate.json")

    if not fiscal_data:
        raise FileNotFoundError("government_fiscal.json not found — run 02_collect_budget.py first")

    # Format fiscal data as a readable table for the LLM.
    # Group by year and variable for easy reading.
    # We send all 1,826 records — at ~100 chars each, that's ~183K chars ≈ 46K tokens.
    # Well within Gemini's 1M token limit.
    fiscal_lines = []
    for record in fiscal_data:
        year = record.get("year", "?")
        variable = record.get("variable", "?")
        value = record.get("value", record.get("amount", "?"))
        fiscal_lines.append(f"  {year} | {variable} | {value}")

    fiscal_text = "\n".join(fiscal_lines)

    # Format tax rate data.
    tax_lines = []
    if tax_data:
        for record in tax_data:
            year = record.get("year", "?")
            rate = record.get("value", record.get("rate", "?"))
            tax_lines.append(f"  {year} | Property Tax Rate per $100 | {rate}")

    tax_text = "\n".join(tax_lines) if tax_lines else "No tax rate data available."

    prompt = f"""You are analyzing fiscal data for {cfg.city_name_long} to prepare a budget
briefing for a newly elected {cfg.member_title.lower()}.

The data comes from {cfg.state_name} LINC/OSBM (Local Government Information Network for Communities)
and covers {cfg.city_name}'s municipal finances.

GOVERNMENT FISCAL DATA ({len(fiscal_data)} records):
Format: Year | Variable | Value

{fiscal_text}

PROPERTY TAX RATES ({len(tax_data) if tax_data else 0} records):
{tax_text}

TASK:
1. Write a 2-3 paragraph SUMMARY of {cfg.city_name}'s fiscal picture. What's the overall
   financial health of the city? How has it changed over time?

2. Identify REVENUE TRENDS:
   - Which revenue sources are growing fastest?
   - Which are declining or flat?
   - How diversified are {cfg.city_name}'s revenue sources?

3. Identify EXPENDITURE TRENDS:
   - Which spending categories have grown most?
   - What's happening with public safety spending?
   - How has debt service changed?

4. Analyze the PROPERTY TAX RATE trend:
   - How has the rate changed over the period covered by the data?
   - How does the rate relate to total revenue? (look for any divergence between rate changes and revenue changes)

5. Provide PER CAPITA analysis if the data includes population figures.

6. Identify CONCERNS that a new council member should be aware of.

7. Identify STRENGTHS in {cfg.city_name}'s fiscal position.

Focus on the most recent 10 years but reference long-term trends for context.
Use specific dollar amounts and percentages where possible."""

    print(f"  Sending {len(fiscal_data)} fiscal records + {len(tax_data) if tax_data else 0} tax records to LLM ({len(prompt)} chars)...")

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=BudgetAnalysis,
        temperature=TEMPERATURE,
        # Use dynamic thinking for budget analysis — it involves reasoning about numbers.
        thinking_budget=-1,
        trace_name="pass3_budget_analysis",
    )

    return result


def run_pass_4(client: GeminiClient) -> DocumentAnalysis:
    """
    Pass 4: Key Documents Deep Dive — Summarize the most important staff reports.

    Input: Extracted PDF text from the most substantive matters.
    Task: Summarize each document's key issue, fiscal impact, and recommendation.

    We select the top ~50 documents by:
    1. Excluding routine types (Consent Items, Nominations, Awards)
    2. Prioritizing Policy Items, Business Items, Public Hearing Items
    3. Picking the longest/most substantive documents within each category

    Estimated tokens: ~200K input, ~15K output ≈ $0.020
    """
    print("  Loading matters and extracted documents...")
    matters = load_json(DATA_DIR / "legistar" / "matters.json")
    if not matters:
        raise FileNotFoundError("matters.json not found")

    # Build a lookup table: matter_id → matter metadata.
    # This lets us quickly find the title/type for any matter by its ID.
    # dict comprehension: {key: value for item in iterable}
    # This is like Object.fromEntries() in JavaScript.
    matter_lookup = {
        str(m["MatterId"]): m
        for m in matters
    }

    # Find all extracted document JSON files.
    extracted_dir = DATA_DIR / "extracted"
    if not extracted_dir.exists():
        raise FileNotFoundError("extracted/ not found — run 03_extract_pdfs.py first")

    extracted_files = sorted(extracted_dir.glob("*.json"))
    print(f"  Found {len(extracted_files)} extracted documents")

    # Routine matter types that don't need deep analysis.
    # These are formulaic items that don't add much insight for a briefing.
    SKIP_TYPES = {
        "Consent Item",
        "Consent - Property Transaction",
        "Nomination",
        "Awards and Recognitions",
    }

    # Priority matter types that we want to analyze first.
    # These contain the most substantive policy content.
    PRIORITY_TYPES = {
        "Policy Item",
        "Business Item",
        "Public Hearing Item",
        "Action Review",
    }

    # Score each document to decide which ones to analyze.
    # Higher score = more important to include.
    scored_docs = []

    for doc_path in extracted_files:
        # Parse the filename to get the matter ID.
        # Filenames are like "28155_43965.json" → matter_id = "28155"
        # .stem gets the filename without extension: "28155_43965"
        # .split("_")[0] gets the part before the underscore: "28155"
        parts = doc_path.stem.split("_")
        matter_id = parts[0] if parts else None

        # Look up the matter metadata.
        matter = matter_lookup.get(matter_id, {})
        matter_type = matter.get("MatterTypeName", "Unknown")
        matter_title = matter.get("MatterTitle", "Unknown")

        # Skip routine types.
        if matter_type in SKIP_TYPES:
            continue

        # Load the extracted document to check its size.
        # We want documents with actual content (not empty or tiny files).
        doc_data = load_json(doc_path)
        if not doc_data:
            continue

        # Get the character count from metadata.
        total_chars = doc_data.get("metadata", {}).get("total_chars", 0)

        # Skip documents with very little text (empty or mostly images).
        if total_chars < 500:
            continue

        # Score: priority types get +1000, then add character count.
        # This ensures priority types sort first, then longest docs within each type.
        score = total_chars
        if matter_type in PRIORITY_TYPES:
            score += 100_000

        scored_docs.append({
            "path": doc_path,
            "matter_id": matter_id,
            "matter_title": matter_title,
            "matter_type": matter_type,
            "total_chars": total_chars,
            "score": score,
        })

    # Sort by score (highest first) and take the top N.
    # key=lambda x: x["score"] tells sorted() to sort by the "score" field.
    # reverse=True sorts descending (highest score first).
    scored_docs.sort(key=lambda x: x["score"], reverse=True)
    selected_docs = scored_docs[:MAX_DOCUMENTS_TO_ANALYZE]

    print(f"  Selected {len(selected_docs)} documents for analysis (from {len(scored_docs)} candidates)")

    # Build a single prompt with all selected documents.
    # We truncate each document to ~8000 chars to fit everything in one LLM call.
    # 50 docs × 8K chars = 400K chars ≈ 100K tokens — fits in Gemini's 1M limit.
    MAX_CHARS_PER_DOC = 8000

    doc_sections = []
    for i, doc_info in enumerate(selected_docs):
        doc_data = load_json(doc_info["path"])
        if not doc_data:
            continue

        full_text = doc_data.get("full_text", "")

        # Truncate long documents. [:N] is Python's slice syntax — takes the first N chars.
        if len(full_text) > MAX_CHARS_PER_DOC:
            full_text = full_text[:MAX_CHARS_PER_DOC] + "\n[... truncated ...]"

        doc_sections.append(
            f"--- DOCUMENT {i + 1} ---\n"
            f"File: {doc_info['path'].name}\n"
            f"Matter: {doc_info['matter_title']}\n"
            f"Type: {doc_info['matter_type']}\n"
            f"Text ({doc_info['total_chars']} chars):\n{full_text}\n"
        )

    documents_text = "\n\n".join(doc_sections)

    prompt = f"""You are analyzing staff reports and supporting documents from {cfg.city_name_full} {cfg.governing_body}
to prepare a briefing for a newly elected {cfg.member_title.lower()}.

Below are {len(selected_docs)} key documents (staff reports, presentations, analyses) from the most
substantive legislative matters in the past 6 months.

{documents_text}

TASK:
For each document, provide:
1. A 2-3 sentence SUMMARY of what the document is about
2. The KEY ISSUE or decision at stake
3. The FISCAL IMPACT (dollar amounts, if mentioned)
4. The STAFF RECOMMENDATION (what staff is asking council to do)

Then identify CROSS-CUTTING THEMES — issues that appear across multiple documents.

Use the exact filename and matter title in your response so we can link back to the source."""

    print(f"  Sending {len(selected_docs)} documents to LLM ({len(prompt)} chars)...")

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=DocumentAnalysis,
        temperature=TEMPERATURE,
        thinking_budget=-1,  # Dynamic thinking — documents need more reasoning.
        trace_name="pass4_document_summaries",
    )

    return result


def run_pass_5(client: GeminiClient) -> CommitteeAnalysis:
    """
    Pass 5: Committee Structure & Activity — Profile each committee.

    Input: Legislative bodies + event items grouped by body.
    Task: Describe each committee's role, meeting frequency, and focus areas.
    Estimated tokens: ~30K input, ~5K output ≈ $0.004
    """
    print("  Loading bodies and event data...")
    bodies = load_json(DATA_DIR / "legistar" / "bodies.json")
    events = load_json(DATA_DIR / "legistar" / "events.json")
    matters = load_json(DATA_DIR / "legistar" / "matters.json")

    if not bodies:
        raise FileNotFoundError("bodies.json not found")

    # Count meetings per body from events data.
    # defaultdict would be more Pythonic, but a regular dict is clearer for learning.
    meeting_counts = {}
    if events:
        for event in events:
            body_name = event.get("EventBodyName", "Unknown")
            # dict.get(key, default) returns the current count or 0 if not found.
            meeting_counts[body_name] = meeting_counts.get(body_name, 0) + 1

    # Group matters by body.
    matters_by_body = {}
    if matters:
        for m in matters:
            body = m.get("MatterBodyName", "Unknown")
            if body not in matters_by_body:
                matters_by_body[body] = []
            matters_by_body[body].append(m.get("MatterTitle", "Untitled"))

    # Build the prompt.
    body_sections = []
    for body in bodies:
        name = body.get("BodyName", "Unknown")
        body_type = body.get("BodyTypeName", "Unknown")
        active = body.get("BodyActiveFlag", 0)

        meetings = meeting_counts.get(name, 0)
        body_matters = matters_by_body.get(name, [])

        # Only include bodies that have meetings or matters.
        if meetings == 0 and not body_matters:
            continue

        # Take the first 15 matter titles as a sample.
        sample_titles = body_matters[:15]
        titles_text = "\n".join(f"    - {t}" for t in sample_titles)
        if len(body_matters) > 15:
            titles_text += f"\n    ... and {len(body_matters) - 15} more"

        body_sections.append(
            f"BODY: {name}\n"
            f"  Type: {body_type}\n"
            f"  Active: {'Yes' if active else 'No'}\n"
            f"  Meetings (6 months): {meetings}\n"
            f"  Matters: {len(body_matters)}\n"
            f"  Sample legislation:\n{titles_text}\n"
        )

    bodies_text = "\n\n".join(body_sections)

    prompt = f"""You are analyzing the committee structure of {cfg.city_name_full} {cfg.governing_body} to prepare
a briefing for a newly elected {cfg.member_title.lower()}.

Below are the legislative bodies (committees, councils, workshops) with their recent meeting
activity and legislation.

{bodies_text}

TASK:
1. For each active body/committee, describe:
   - Its ROLE (what is this committee responsible for?)
   - KEY TOPICS it has been working on (based on the legislation titles)
   - RECENT FOCUS (what's dominating their agenda lately?)
   - UPCOMING ISSUES (what should a new member expect to see next?)

2. Provide STRUCTURAL OBSERVATIONS:
   - How does {cfg.city_name}'s committee system work?
   - Which committees are most active?
   - How do items flow from committee to full {cfg.governing_body_short}?
   - Are there any committees that seem to overlap in jurisdiction?

A new {cfg.member_title.lower()} needs to understand which committees they might serve on
and what those committees actually do day-to-day."""

    print(f"  Sending {len(body_sections)} body profiles to LLM ({len(prompt)} chars)...")

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=CommitteeAnalysis,
        temperature=TEMPERATURE,
        thinking_budget=DEFAULT_THINKING_BUDGET,
        trace_name="pass5_committee_analysis",
    )

    return result


def run_pass_6(client: GeminiClient) -> Synthesis:
    """
    Pass 6: Synthesis — Cross-reference all analysis into themes and priorities.

    Input: Results from passes 1-5.
    Task: Identify overarching themes, priorities, and advice for a new member.

    This is the most important pass — it ties everything together into actionable insight.
    We use dynamic thinking (-1) here because synthesis requires the most reasoning.

    Estimated tokens: ~80K input, ~10K output ≈ $0.009
    """
    print("  Loading results from passes 1-5...")

    # Load all previous pass results.
    # We use the same save_result format, so they're all JSON files.
    pass1 = load_json(OUTPUT_DIR / "pass1_legislative_overview.json")
    pass2 = load_json(OUTPUT_DIR / "pass2_vote_analysis.json")
    pass3 = load_json(OUTPUT_DIR / "pass3_budget_analysis.json")
    pass4 = load_json(OUTPUT_DIR / "pass4_document_summaries.json")
    pass5 = load_json(OUTPUT_DIR / "pass5_committee_analysis.json")

    # Check that we have all previous results.
    # all() returns True if every element is truthy (not None, not empty, etc.)
    if not all([pass1, pass2, pass3, pass4, pass5]):
        missing = []
        if not pass1: missing.append("pass1")
        if not pass2: missing.append("pass2")
        if not pass3: missing.append("pass3")
        if not pass4: missing.append("pass4")
        if not pass5: missing.append("pass5")
        raise FileNotFoundError(f"Missing results from: {', '.join(missing)}. Run those passes first.")

    # json.dumps() with indent makes the JSON human-readable.
    # We send all results to the LLM for cross-referencing.
    prompt = f"""You are preparing the final synthesis of a comprehensive briefing for a newly
elected {cfg.city_name_full} {cfg.governing_body} member. You have analysis from 5 previous research passes.
Your job is to synthesize all of this into a coherent, actionable briefing.

=== PASS 1: LEGISLATIVE OVERVIEW ===
{json.dumps(pass1, indent=2)}

=== PASS 2: VOTING & DECISION ANALYSIS ===
{json.dumps(pass2, indent=2)}

=== PASS 3: BUDGET & FISCAL ANALYSIS ===
{json.dumps(pass3, indent=2)}

=== PASS 4: KEY DOCUMENT SUMMARIES ===
{json.dumps(pass4, indent=2)}

=== PASS 5: COMMITTEE STRUCTURE ===
{json.dumps(pass5, indent=2)}

TASK — SYNTHESIZE AND ADVISE:

1. Write an EXECUTIVE SUMMARY (3-5 paragraphs) that a busy new {cfg.member_title.lower()} could read
   in 5 minutes and understand the state of {cfg.city_name} {cfg.entity_type} government. Cover the biggest
   themes, the fiscal picture, and what the council has been focused on.

2. Identify 5-8 KEY THEMES that cut across all the data:
   - Each theme should have a clear title, description, supporting evidence from the data,
     and an explanation of why it matters for a new {cfg.member_title.lower()}.
   - Themes should be specific to {cfg.city_name} (not generic like "cities face budget challenges").

3. Provide IMMEDIATE PRIORITIES — what should this {cfg.member_title.lower()} focus on in their first
   90 days? What meetings should they attend? What topics should they study?

4. Identify ONGOING ISSUES that will continue to require attention over the full term.

5. Suggest RELATIONSHIPS TO BUILD — who are the key stakeholders, partner organizations,
   or fellow {cfg.governing_body_short} members this person should connect with?

6. Note any KNOWLEDGE GAPS — areas where the data was limited and the {cfg.member_title.lower()}
   should seek additional information.

Write for someone who is smart but new to {cfg.entity_type} government. Avoid jargon. Be specific —
use actual names, dollar amounts, and matter titles from the data."""

    print(f"  Sending synthesis prompt to LLM ({len(prompt)} chars)...")

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=Synthesis,
        temperature=0.3,  # Slightly higher temperature for more natural prose.
        thinking_budget=-1,  # Dynamic thinking — synthesis is the hardest task.
        trace_name="pass6_synthesis",
    )

    return result


# ============================================================================
# MAIN RUNNER
# ============================================================================

def run_all_passes():
    """
    Run all 6 analysis passes sequentially.

    Each pass:
    1. Checks if its output already exists (skip if so)
    2. Loads relevant data
    3. Sends a prompt to the LLM
    4. Saves the structured result as JSON
    5. Reports time and cost for this pass
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Check for API key before starting.
    if not os.getenv("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY not found in environment.")
        print("Add it to your .env file: GEMINI_API_KEY=your_key_here")
        sys.exit(1)

    print("=" * 60)
    print(f"{cfg.city_name} POC — LLM Analysis Pipeline")
    print("=" * 60)
    print(f"Model:  Gemini 2.5 Flash")
    print(f"Input:  {DATA_DIR.resolve()}")
    print(f"Output: {OUTPUT_DIR.resolve()}")
    print()

    # Initialize the Gemini client.
    # This creates an HTTP connection pool and validates the API key.
    client = GeminiClient(
        default_model=MODEL,
        default_temperature=TEMPERATURE,
        thinking_budget=DEFAULT_THINKING_BUDGET,
        # Low connection limits — we make sequential calls, not parallel.
        max_connections=10,
        max_keepalive_connections=5,
        max_retries=3,
    )

    # Define the passes as a list of tuples: (name, function, output_filename).
    # This lets us loop through them cleanly instead of repeating code for each pass.
    # In Python, functions are "first-class objects" — you can store them in variables,
    # put them in lists, and pass them as arguments. This is like passing function
    # references in JavaScript.
    passes = [
        ("Pass 1: Legislative Overview",     run_pass_1, "pass1_legislative_overview"),
        ("Pass 2: Vote & Decision Analysis",  run_pass_2, "pass2_vote_analysis"),
        ("Pass 3: Budget & Fiscal Context",   run_pass_3, "pass3_budget_analysis"),
        ("Pass 4: Key Documents Deep Dive",   run_pass_4, "pass4_document_summaries"),
        ("Pass 5: Committee Structure",       run_pass_5, "pass5_committee_analysis"),
        ("Pass 6: Synthesis",                 run_pass_6, "pass6_synthesis"),
    ]

    # Track overall timing.
    total_start = time.time()
    passes_run = 0
    passes_skipped = 0

    for pass_name, pass_fn, output_filename in passes:
        print(f"\n{'─' * 60}")
        print(f"  {pass_name}")
        print(f"{'─' * 60}")

        # Skip if already done.
        if pass_already_done(output_filename):
            print(f"  SKIP: {output_filename}.json already exists (delete to re-run)")
            passes_skipped += 1
            continue

        pass_start = time.time()

        try:
            # Call the pass function. It returns a Pydantic model or dict.
            result = pass_fn(client)

            # Save the result.
            out_path = save_result(result, output_filename)
            pass_elapsed = time.time() - pass_start

            # Get cost stats from the client.
            # Note: this shows CUMULATIVE stats, not per-pass.
            stats = client.get_usage_stats()

            print(f"  DONE: Saved to {out_path.name}")
            print(f"  Time: {pass_elapsed:.1f}s")
            print(f"  Cost so far: ${stats['total_cost']:.4f} ({stats['api_call_count']} API calls)")

            passes_run += 1

        except Exception as e:
            # If a pass fails, print the error and continue to the next pass.
            # This way one failure doesn't stop the entire pipeline.
            print(f"  ERROR: {pass_name} failed: {e}")
            print(f"  Continuing to next pass...")

            # If it's a critical error (like no API key), we might want to stop.
            # isinstance() checks if the exception is a specific type.
            if isinstance(e, (FileNotFoundError, ValueError)):
                # Data missing — later passes might fail too.
                print(f"  WARNING: Data dependency issue — later passes may also fail.")

    # ---------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------
    total_elapsed = time.time() - total_start
    stats = client.get_usage_stats()

    print()
    print("=" * 60)
    print("Analysis Pipeline Complete!")
    print("=" * 60)
    print(f"  Passes run:        {passes_run}")
    print(f"  Passes skipped:    {passes_skipped}")
    print(f"  Total time:        {total_elapsed:.1f}s ({total_elapsed / 60:.1f} min)")
    print(f"  API calls:         {stats['api_call_count']}")
    print(f"  Prompt tokens:     {stats['total_prompt_tokens']:,}")
    print(f"  Completion tokens: {stats['total_completion_tokens']:,}")
    print(f"  Total cost:        ${stats['total_cost']:.4f}")
    if stats['api_call_count'] > 0:
        print(f"  Avg cost/call:     ${stats['total_cost'] / stats['api_call_count']:.4f}")
    print(f"  Output dir:        {OUTPUT_DIR.resolve()}")
    print("=" * 60)


# ============================================================================
# ENTRY POINT
# ============================================================================

# __name__ == "__main__" means this code only runs when you execute this file directly,
# not when it's imported by another module.
if __name__ == "__main__":
    run_all_passes()
