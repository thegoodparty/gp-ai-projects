"""
06b_map_topics_to_issues.py — Auto-generate the topic-to-issue mapping via LLM.

This script bridges the council's legislative topic areas (from Pass 1 of
script 04) to Haystaq voter score columns, enabling the mismatch computation
in script 07. Instead of maintaining this mapping manually in city_config.json,
an LLM reads the topic descriptions and the available Haystaq columns, then
produces the mapping automatically.

Why LLM?
  The council topic names are themselves LLM-generated (by Pass 1) and vary
  per city. Manually mapping them to Haystaq columns for each new city would
  be tedious and error-prone. An LLM can reason about semantic similarity
  between "Housing and Community Development" and "hs_affordable_housing_gov_has_role".

Prerequisites:
  - data/analysis/pass1_legislative_overview.json (from script 04)
  - city_config.json with haystaq.issue_scores defined
  - GEMINI_API_KEY in .env

Usage:
    python scripts/06b_map_topics_to_issues.py

Output:
    data/analysis/topic_to_issue_map.json
"""

# --- Standard library imports ---
import json
from pathlib import Path

# --- Third-party imports ---
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# --- Project imports ---
from shared.llm_gemini import GeminiClient, GeminiModelType
from city_config import cfg

# Load environment variables (.env) for GEMINI_API_KEY.
load_dotenv()


# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir
ANALYSIS_DIR = DATA_DIR / "analysis"

# Input file: Pass 1 legislative overview (has topic_areas with names + descriptions).
PASS1_PATH = ANALYSIS_DIR / "pass1_legislative_overview.json"

# Output file: the auto-generated mapping.
OUTPUT_PATH = ANALYSIS_DIR / "topic_to_issue_map.json"

MODEL = GeminiModelType.FLASH
TEMPERATURE = 0.1  # Low temperature for deterministic mapping.
THINKING_BUDGET = 8192


# ============================================================================
# PYDANTIC SCHEMA — constrains the LLM output
# ============================================================================

class TopicMapping(BaseModel):
    """A single mapping from one council topic to zero or more Haystaq columns."""
    topic_name: str = Field(description="Exact topic name from Pass 1 legislative overview")
    haystaq_columns: list[str] = Field(
        description="List of Haystaq column names that are semantically relevant to this topic. "
                    "Empty list if no Haystaq column is a good match."
    )
    reasoning: str = Field(description="Brief explanation of why these columns were chosen (or why none matched)")


class TopicToIssueMap(BaseModel):
    """Complete mapping of all council topics to Haystaq voter score columns."""
    mappings: list[TopicMapping] = Field(description="One entry per council topic area from Pass 1")


# ============================================================================
# PROMPT CONSTRUCTION
# ============================================================================

def build_prompt(pass1: dict) -> str:
    """Build the LLM prompt from Pass 1 topics and Haystaq column definitions."""

    # Format the council topics.
    topic_lines = []
    for topic in pass1.get("topic_areas", []):
        topic_lines.append(
            f"- **{topic['name']}** ({topic['matter_count']} matters): {topic['description']}"
        )
    topics_text = "\n".join(topic_lines)

    # Format the available Haystaq columns.
    column_lines = []
    for col_name, display_name in cfg.haystaq_issue_scores.items():
        column_lines.append(f"- `{col_name}` — {display_name}")
    columns_text = "\n".join(column_lines)

    return f"""You are a political data analyst. Your task is to map legislative topic areas
from a city council's agenda to voter attitude survey columns from Haystaq.

## Council Topic Areas (from legislative analysis)

These are the topic areas that the {cfg.city_name} council has been working on:

{topics_text}

## Available Haystaq Voter Score Columns

These are the voter attitude columns we have data for. Each column contains a
predictive score (0-100) indicating how strongly voters in {cfg.city_name} feel
about that issue:

{columns_text}

## Your Task

For EACH council topic area listed above, determine which Haystaq columns (if any)
are semantically relevant. A column is relevant if the voter attitude it measures
directly relates to the policy decisions the council is making in that topic area.

Rules:
1. Each Haystaq column can map to AT MOST one topic (no duplicates across topics).
2. A topic can map to zero, one, or multiple columns.
3. Only map columns where there is a clear, direct semantic connection.
4. Administrative/operational topics (e.g., "General City Operations") will often
   have no matching voter columns — that's expected.
5. Be conservative: only map when the connection is clear, not speculative.
6. Use the EXACT topic names and column names as shown above."""


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print(f"06b — Auto-generate Topic-to-Issue Mapping")
    print("=" * 60)
    print(f"City:   {cfg.city_name_full}")
    print(f"Input:  {PASS1_PATH.name}")
    print(f"Output: {OUTPUT_PATH.name}")
    print()

    # ── Load Pass 1 ──────────────────────────────────────────────────
    if not PASS1_PATH.exists():
        print(f"ERROR: {PASS1_PATH} not found — run script 04 first.")
        return

    with open(PASS1_PATH, "r", encoding="utf-8") as f:
        pass1 = json.load(f)

    topic_count = len(pass1.get("topic_areas", []))
    print(f"  Loaded {topic_count} topic areas from Pass 1")
    print(f"  Available Haystaq columns: {len(cfg.haystaq_issue_scores)}")
    print()

    # ── Build prompt and call LLM ────────────────────────────────────
    prompt = build_prompt(pass1)
    print(f"  Prompt length: {len(prompt):,} chars")
    print(f"  Calling Gemini {MODEL.value}...")

    client = GeminiClient(
        default_model=MODEL,
        default_temperature=TEMPERATURE,
        thinking_budget=THINKING_BUDGET,
        max_connections=5,
        max_keepalive_connections=3,
        max_retries=3,
    )

    result = client.generate_structured_content(
        prompt=prompt,
        response_schema=TopicToIssueMap,
        temperature=TEMPERATURE,
        thinking_budget=THINKING_BUDGET,
        trace_name="06b_topic_to_issue_map",
    )

    # ── Parse result ─────────────────────────────────────────────────
    # generate_structured_content may return a Pydantic model or a dict.
    if isinstance(result, TopicToIssueMap):
        mappings = result.mappings
    elif isinstance(result, dict):
        mappings = [TopicMapping(**m) for m in result.get("mappings", [])]
    else:
        print(f"ERROR: Unexpected result type: {type(result)}")
        return

    print(f"\n  LLM returned {len(mappings)} topic mappings:")
    print()

    # ── Build the output map (topic_name → [column_names]) ───────────
    topic_map: dict[str, list[str]] = {}
    for mapping in mappings:
        topic_map[mapping.topic_name] = mapping.haystaq_columns
        cols_display = ", ".join(mapping.haystaq_columns) if mapping.haystaq_columns else "(none)"
        print(f"    {mapping.topic_name}")
        print(f"      → {cols_display}")
        print(f"      Reasoning: {mapping.reasoning}")
        print()

    # ── Summary stats ────────────────────────────────────────────────
    mapped_count = sum(1 for cols in topic_map.values() if cols)
    unmapped_count = sum(1 for cols in topic_map.values() if not cols)
    total_columns_used = sum(len(cols) for cols in topic_map.values())

    print(f"  Summary:")
    print(f"    Topics with voter mapping:    {mapped_count}")
    print(f"    Topics without mapping:       {unmapped_count}")
    print(f"    Total Haystaq columns used:   {total_columns_used} / {len(cfg.haystaq_issue_scores)}")

    # Check for duplicate column assignments.
    all_assigned = []
    for cols in topic_map.values():
        all_assigned.extend(cols)
    duplicates = [c for c in set(all_assigned) if all_assigned.count(c) > 1]
    if duplicates:
        print(f"    WARNING: Duplicate column assignments: {duplicates}")

    # Check for invalid column names.
    valid_columns = set(cfg.haystaq_issue_scores.keys())
    invalid = [c for c in all_assigned if c not in valid_columns]
    if invalid:
        print(f"    WARNING: Invalid column names: {invalid}")

    # ── Save output ──────────────────────────────────────────────────
    output = {
        "city": cfg.city_name_full,
        "generated_by": "06b_map_topics_to_issues.py",
        "model": MODEL.value,
        "topic_count": topic_count,
        "topic_to_issue_map": topic_map,
        "detailed_mappings": [
            {
                "topic_name": m.topic_name,
                "haystaq_columns": m.haystaq_columns,
                "reasoning": m.reasoning,
            }
            for m in mappings
        ],
    }

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\n  Saved to {OUTPUT_PATH}")
    print("\nDone!")


if __name__ == "__main__":
    main()
