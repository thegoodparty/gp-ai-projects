"""
10_chat.py — Interactive conversational briefing prototype (Gemini Flash).

Loads all pre-computed analysis data as system context (~155K tokens),
defines tool functions for raw data drill-down, and runs an interactive
terminal conversation using Gemini 2.5 Flash.

Usage:
    uv run python briefing_poc/scripts/10_chat.py --city charlotte
    uv run python briefing_poc/scripts/10_chat.py --city raleigh

Requires GEMINI_API_KEY in .env or environment.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown

# Load .env from briefing_poc root and workspace root
_briefing_root = Path(__file__).resolve().parent.parent
load_dotenv(_briefing_root / ".env")
load_dotenv(_briefing_root.parent / ".env")

from google import genai
from google.genai import types

from city_config import cfg

console = Console()


# ============================================================================
# DATA LOADING — Tier 1 (system context)
# ============================================================================

# Files to load into system context, in order.
# (label, path relative to cfg.data_dir)
CONTEXT_FILES = [
    ("LEGISLATIVE OVERVIEW (Pass 1)", "analysis/pass1_legislative_overview.json"),
    ("VOTE ANALYSIS (Pass 2)", "analysis/pass2_vote_analysis.json"),
    ("BUDGET ANALYSIS (Pass 3)", "analysis/pass3_budget_analysis.json"),
    ("DOCUMENT SUMMARIES (Pass 4)", "analysis/pass4_document_summaries.json"),
    ("COMMITTEE ANALYSIS (Pass 5)", "analysis/pass5_committee_analysis.json"),
    ("SYNTHESIS (Pass 6)", "analysis/pass6_synthesis.json"),
    ("CONSTITUENT DEMOGRAPHICS", "constituent/demographics.json"),
    ("CONSTITUENT ISSUE SCORES", "constituent/issue_scores.json"),
    ("CONSTITUENT ZIP BREAKDOWN", "constituent/zip_breakdown.json"),
    ("CONSTITUENT SUMMARY", "constituent/constituent_summary.json"),
    ("COUNCIL VS. CONSTITUENT MISMATCH", "analysis/council_vs_constituent.json"),
    ("QUICK WINS", "analysis/quick_wins.json"),
    ("TOPIC-TO-ISSUE MAPPING", "analysis/topic_to_issue_map.json"),
    ("DISCUSSION NARRATIVES", "discussions/discussion_narratives.json"),
]


def load_context_data() -> tuple[str, int]:
    """Load all analysis JSON files into a single context string.

    Returns (context_string, total_chars).
    """
    sections = []
    total_chars = 0
    loaded = 0
    skipped = []

    for label, rel_path in CONTEXT_FILES:
        filepath = cfg.data_dir / rel_path
        if not filepath.exists():
            skipped.append(rel_path)
            continue
        text = filepath.read_text(encoding="utf-8")
        sections.append(f"=== {label} ===\n{text}")
        total_chars += len(text)
        loaded += 1

    if skipped:
        print(f"[data] Skipped {len(skipped)} missing files: {', '.join(skipped)}")
    print(f"[data] Loaded {loaded} files — {total_chars:,} chars (~{total_chars // 4:,} tokens)")

    return "\n\n".join(sections), total_chars


# ============================================================================
# RAW DATA — loaded once for tool use
# ============================================================================

_matters_cache: list[dict] | None = None


def _load_matters() -> list[dict]:
    """Load matters.json once and cache it."""
    global _matters_cache
    if _matters_cache is None:
        matters_path = cfg.data_dir / "legistar" / "matters.json"
        if matters_path.exists():
            _matters_cache = json.loads(matters_path.read_text(encoding="utf-8"))
            print(f"[data] Loaded {len(_matters_cache)} matters for tool use")
        else:
            _matters_cache = []
            print("[data] No matters.json found — search/detail tools will return empty results")
    return _matters_cache


# ============================================================================
# TOOL FUNCTIONS — Tier 2 (on-demand retrieval)
# ============================================================================

def tool_get_matter_details(matter_id: int) -> dict:
    """Get full details on a specific legislative matter by ID."""
    matters = _load_matters()
    match = next((m for m in matters if m.get("MatterId") == matter_id), None)
    if not match:
        return {"error": f"No matter found with MatterId={matter_id}"}

    # Trim null fields for readability
    result = {k: v for k, v in match.items() if v is not None}

    # Check for attachments
    attachments_dir = cfg.data_dir / "legistar" / "matter_attachments" / str(matter_id)
    if attachments_dir.exists():
        attachment_files = sorted(attachments_dir.iterdir())
        result["_attachments"] = [f.stem for f in attachment_files if f.suffix == ".json"]

    return result


def tool_get_document_text(filename: str) -> dict:
    """Read the extracted text of a staff report or attachment PDF."""
    if not filename.endswith(".json"):
        filename = filename + ".json"
    filepath = cfg.data_dir / "extracted" / filename
    if not filepath.exists():
        return {"error": f"No extracted document found: {filename}"}

    doc = json.loads(filepath.read_text(encoding="utf-8"))
    return {
        "metadata": doc.get("metadata", {}),
        "full_text": doc.get("full_text", ""),
    }


def tool_get_vote_record(event_item_id: int) -> dict:
    """Get the roll-call vote for a specific agenda item."""
    filepath = cfg.data_dir / "legistar" / "votes" / f"{event_item_id}.json"
    if not filepath.exists():
        return {"error": f"No vote record found for EventItemId={event_item_id}. This legislative system may not expose structured vote data."}

    votes = json.loads(filepath.read_text(encoding="utf-8"))
    simplified = [
        {
            "person": v.get("VotePersonName"),
            "vote": v.get("VoteValueName"),
            "result": "Pass" if v.get("VoteResult") == 1 else "Fail" if v.get("VoteResult") == 2 else None,
        }
        for v in votes
    ]
    return {"votes": simplified}


def tool_get_event_items(event_id: int) -> dict:
    """Get all agenda items for a specific meeting/event."""
    filepath = cfg.data_dir / "legistar" / "event_items" / f"{event_id}.json"
    if not filepath.exists():
        return {"error": f"No event items found for EventId={event_id}"}

    items = json.loads(filepath.read_text(encoding="utf-8"))
    simplified = [
        {
            "event_item_id": item.get("EventItemId"),
            "sequence": item.get("EventItemAgendaSequence"),
            "title": item.get("EventItemTitle"),
            "action": item.get("EventItemActionName"),
            "action_text": item.get("EventItemActionText"),
            "passed": item.get("EventItemPassedFlagName"),
            "matter_id": item.get("EventItemMatterId"),
            "matter_file": item.get("EventItemMatterFile"),
            "matter_type": item.get("EventItemMatterType"),
        }
        for item in items
    ]
    return {"items": simplified}


def tool_search_matters(query: str) -> dict:
    """Search legislative matters by keyword (case-insensitive substring match)."""
    matters = _load_matters()
    query_lower = query.lower()
    matches = []

    for m in matters:
        title = (m.get("MatterTitle") or "").lower()
        name = (m.get("MatterName") or "").lower()
        file_num = (m.get("MatterFile") or "").lower()
        if query_lower in title or query_lower in name or query_lower in file_num:
            matches.append({
                "MatterId": m.get("MatterId"),
                "MatterFile": m.get("MatterFile"),
                "MatterTitle": m.get("MatterTitle"),
                "MatterTypeName": m.get("MatterTypeName"),
                "MatterStatusName": m.get("MatterStatusName"),
                "MatterIntroDate": m.get("MatterIntroDate"),
                "MatterBodyName": m.get("MatterBodyName"),
            })

    if not matches:
        return {"message": f"No matters found matching '{query}'", "count": 0}

    return {
        "count": len(matches),
        "showing": min(len(matches), 20),
        "matters": matches[:20],
    }


# Tool dispatch map
TOOL_DISPATCH = {
    "get_matter_details": lambda args: tool_get_matter_details(args["matter_id"]),
    "get_document_text": lambda args: tool_get_document_text(args["filename"]),
    "get_vote_record": lambda args: tool_get_vote_record(args["event_item_id"]),
    "get_event_items": lambda args: tool_get_event_items(args["event_id"]),
    "search_matters": lambda args: tool_search_matters(args["query"]),
}


# ============================================================================
# TOOL DEFINITIONS — Gemini FunctionDeclaration format
# ============================================================================

TOOL_DECLARATIONS = types.Tool(function_declarations=[
    types.FunctionDeclaration(
        name="get_matter_details",
        description="Get full details on a specific legislative matter by its ID. Returns the matter record including title, type, status, dates, and attachment list. Use this when you need specifics about a matter referenced in the analysis.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "matter_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="The MatterId (numeric ID) of the legislative matter",
                ),
            },
            required=["matter_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_document_text",
        description="Read the full extracted text of a staff report or attachment PDF. The filename format is '{matter_id}_{attachment_id}' (without extension). Use this when the official wants to read the actual content of a document referenced in the analysis.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "filename": types.Schema(
                    type=types.Type.STRING,
                    description="The document filename (e.g., '28155_43965'). Found in matter attachment lists.",
                ),
            },
            required=["filename"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_vote_record",
        description="Get the roll-call vote for a specific agenda item, showing how each council member voted. Use this when the official asks about voting patterns or how specific members voted.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "event_item_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="The EventItemId of the agenda item to look up votes for",
                ),
            },
            required=["event_item_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="get_event_items",
        description="Get all agenda items for a specific meeting/event. Use this when the official wants to see what else was on the agenda for a particular meeting.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "event_id": types.Schema(
                    type=types.Type.INTEGER,
                    description="The EventId of the meeting/event",
                ),
            },
            required=["event_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="search_matters",
        description="Search legislative matters by keyword. Searches matter titles, names, and file numbers. Returns up to 20 matching results. Use this when the official asks about a topic and you need to find specific legislation.",
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties={
                "query": types.Schema(
                    type=types.Type.STRING,
                    description="Search keyword(s) to match against matter titles and names",
                ),
            },
            required=["query"],
        ),
    ),
])


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

def build_system_prompt(context_data: str) -> str:
    """Build the full system prompt with role, data, and tool guidance."""

    if cfg.legislative_system == "legistar":
        citation_note = f"When citing specific matters, include the Legistar URL: {cfg.legistar_gateway_pattern}"
    elif cfg.legislative_system == "escribemeetings":
        citation_note = f"When citing specific matters, reference the eSCRIBE Meetings portal: {cfg.escribemeetings_base_url}"
    else:
        citation_note = "Include source references when citing specific matters."

    return f"""You are an AI assistant helping a newly elected official understand their municipality. You have comprehensive analysis of {cfg.city_name_long}'s government data covering {cfg.data_period_display}.

ABOUT THIS DATA:
- The {cfg.governing_body} ({cfg.city_name_full}) legislative data covers {cfg.data_period_display}
- Constituent data is based on {cfg.city_name} registered voters from Haystaq/L2 voter modeling
- Voter priority scores are modeled estimates (0-100 scale) based on voter file demographics, consumer data, and survey calibration — not direct survey responses
- Budget data comes from the NC LINC public database ({cfg.budget_government_url})
- {citation_note}

YOUR ROLE:
Guide the official through understanding their municipality, following this recommended flow — but let them go deeper or skip ahead at any point:

1. **Who are your constituents?** — Demographics, voter registration, ideology breakdown
2. **What do they care about?** — Top issue priorities (scored 0-100), tiered by urgency
3. **Where is the {cfg.governing_body_short} misaligned with constituents?** — Topics where {cfg.governing_body_short} time allocation doesn't match voter priorities
4. **What budget challenges should you know?** — Fiscal trends, revenue/expenditure patterns, key documents
5. **What can you do right now?** — Quick wins: specific, actionable recommendations tied to constituent gaps
6. **What's the bigger picture?** — Key themes, ongoing issues, relationships to build

IMPORTANT GUIDELINES:
- Start by briefly introducing yourself and asking what they'd like to focus on, or offering to walk them through the overview
- When presenting data, be conversational — don't dump raw JSON. Synthesize and highlight what matters
- When citing numbers, reference the source (e.g., "According to the budget analysis..." or "Based on voter data...")
- If challenged on a finding, trace the evidence chain: what data supports it, where it came from, and how they can verify it
- For voter priority scores, be transparent that these are modeled estimates, not direct surveys
- Use tools to drill into specific matters, votes, or documents when the official wants details beyond the analysis summaries
- Most questions can be answered from the analysis data below — only call tools when you need specific raw data (individual matter details, full document text, roll-call votes, or keyword search)

ANALYSIS DATA:

{context_data}"""


# ============================================================================
# CONVERSATION LOOP
# ============================================================================

def run_chat():
    """Main conversation loop."""
    # Load data
    print(f"\n[chat] Loading analysis data for {cfg.city_name_full}...")
    context_data, total_chars = load_context_data()

    # Pre-load matters for tool use
    _load_matters()

    # Build system prompt
    system_prompt = build_system_prompt(context_data)
    system_chars = len(system_prompt)
    print(f"[chat] System prompt: {system_chars:,} chars (~{system_chars // 4:,} tokens)")

    # Initialize Gemini client
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("\nError: GEMINI_API_KEY environment variable not set.")
        print("Add it to .env or set it with: export GEMINI_API_KEY=...")
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash"

    # Chat config with tools
    chat_config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        tools=[TOOL_DECLARATIONS],
        temperature=0.7,
    )

    # Create a chat session — manages conversation history automatically
    chat = client.chats.create(model=model, config=chat_config)

    print(f"\nBriefing Chat — {cfg.city_name_full}")
    print(f"Data period: {cfg.data_period_display}")
    print(f"Model: {model}")
    print(f"Type 'quit' to exit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nGoodbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("\nGoodbye!")
            break

        try:
            response = chat.send_message(user_input)
        except Exception as e:
            print(f"\nAPI error: {e}")
            continue

        # Handle function calls — Gemini may request tool use
        while response.candidates and response.candidates[0].content.parts:
            # Check if any part is a function call
            function_calls = [
                part for part in response.candidates[0].content.parts
                if part.function_call
            ]

            if not function_calls:
                break  # No function calls, we have the final text

            # Execute each function call and build responses
            function_responses = []
            for part in function_calls:
                fc = part.function_call
                tool_name = fc.name
                tool_args = dict(fc.args) if fc.args else {}
                print(f"  [tool] {tool_name}({json.dumps(tool_args, default=str)})")

                if tool_name in TOOL_DISPATCH:
                    try:
                        result = TOOL_DISPATCH[tool_name](tool_args)
                    except Exception as e:
                        result = {"error": f"Tool execution failed: {e}"}
                else:
                    result = {"error": f"Unknown tool: {tool_name}"}

                function_responses.append(
                    types.Part.from_function_response(
                        name=tool_name,
                        response=result,
                    )
                )

            # Send function results back to Gemini
            try:
                response = chat.send_message(function_responses)
            except Exception as e:
                print(f"\nAPI error during tool use: {e}")
                break

        # Extract and display the final text response (rendered as markdown)
        final_text = response.text if response.text else "(no text response)"
        console.print()
        console.print(Markdown(final_text))
        console.print()

        # Print usage stats
        if hasattr(response, "usage_metadata") and response.usage_metadata:
            usage = response.usage_metadata
            prompt_tokens = getattr(usage, "prompt_token_count", 0)
            output_tokens = getattr(usage, "candidates_token_count", 0)
            print(f"  [{prompt_tokens:,} in / {output_tokens:,} out tokens]\n")


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    run_chat()
