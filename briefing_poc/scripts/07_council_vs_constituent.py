"""
07_council_vs_constituent.py — Compare where the council spends its time vs. what constituents care about.

This is the CPO's "Step 3" insight: overlay the council's legislative agenda
(from Pass 1 of the LLM analysis) with constituent priorities (from Haystaq
voter scores collected in script 06). The result is a mismatch table showing
where the council is over- or under-investing relative to voter concerns.

No LLM needed — this is pure data joining and percentage math.

Example output:
  Housing:   Council 2.3% of matters  |  Voters #1 (score 66.4)  →  UNDER-REPRESENTED
  Transit:   Council 4.1% of matters  |  Voters #2 (score 64.7)  →  UNDER-REPRESENTED
  Zoning:    Council 29.7% of matters |  Voters N/A              →  NO VOTER EQUIVALENT

Prerequisites:
  - data/analysis/pass1_legislative_overview.json (from script 04)
  - data/constituent/issue_scores.json (from script 06)

Usage:
    python scripts/07_council_vs_constituent.py

Output:
    data/analysis/council_vs_constituent.json
"""

# --- Standard library imports ---
import json
from pathlib import Path

# --- Project imports ---
from city_config import cfg
from utils import load_json

# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir
ANALYSIS_DIR = DATA_DIR / "analysis"
CONSTITUENT_DIR = DATA_DIR / "constituent"

# Mapping: council topic area name → list of Haystaq issue column names.
# Prefer the auto-generated file from script 06b (LLM-generated mapping).
# Falls back to the static mapping in city_config.json if the file doesn't exist.
#
# When multiple Haystaq columns map to one council topic, we take the average
# of their scores to get a single "constituent priority" number.
AUTO_MAP_PATH = ANALYSIS_DIR / "topic_to_issue_map.json"

def _load_topic_map() -> dict[str, list[str]]:
    """Load topic-to-issue mapping, preferring auto-generated file from 06b."""
    if AUTO_MAP_PATH.exists():
        with open(AUTO_MAP_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = data.get("topic_to_issue_map", {})
        print(f"  Using auto-generated topic map from {AUTO_MAP_PATH.name} ({len(mapping)} topics)")
        return mapping
    print(f"  No auto-generated map found, using static config from city_config.json")
    return cfg.topic_to_issue_map

TOPIC_TO_ISSUE_MAP = _load_topic_map()

# Display names for Haystaq columns — derived from city_config.json issue_scores.
COLUMN_TO_NAME = cfg.haystaq_issue_scores


# ============================================================================
# ANALYSIS
# ============================================================================

def build_mismatch_table(pass1: dict, issue_scores: dict) -> list[dict]:
    """
    Build the council-vs-constituent mismatch table.

    For each council topic area, compute:
      - council_pct: what % of total matters this topic represents
      - constituent_score: average Haystaq score for mapped issues (0-100)
      - constituent_rank: where this falls in the voter priority ranking
      - gap_type: UNDER (voters care more than council), OVER, MATCH, or UNMAPPED

    Args:
        pass1: The Pass 1 legislative overview JSON.
        issue_scores: The Haystaq issue scores JSON from script 06.

    Returns:
        A list of dicts, one per council topic area, sorted by gap severity.
    """
    # Build a lookup: Haystaq column name → score.
    score_lookup = {}
    for issue in issue_scores.get("issues", []):
        score_lookup[issue["column"]] = issue["score"]

    # Get total matters for percentage calculation.
    total_matters = pass1.get("total_matters", 1)

    results = []

    for topic in pass1.get("topic_areas", []):
        topic_name = topic["name"]
        matter_count = topic["matter_count"]
        council_pct = round((matter_count / total_matters) * 100, 1)

        # Look up the mapped Haystaq issue columns for this council topic.
        mapped_columns = TOPIC_TO_ISSUE_MAP.get(topic_name, [])

        if not mapped_columns:
            # No voter equivalent — this is purely internal/administrative council work.
            results.append({
                "council_topic": topic_name,
                "council_matters": matter_count,
                "council_pct": council_pct,
                "constituent_score": None,
                "constituent_issues": [],
                "constituent_rank": None,
                "gap_type": "UNMAPPED",
                "gap_description": "No direct constituent equivalent — administrative or operational",
            })
            continue

        # Average the mapped scores.
        scores = []
        issue_names = []
        for col in mapped_columns:
            if col in score_lookup:
                scores.append(score_lookup[col])
                issue_names.append(COLUMN_TO_NAME.get(col, col))

        if not scores:
            results.append({
                "council_topic": topic_name,
                "council_matters": matter_count,
                "council_pct": council_pct,
                "constituent_score": None,
                "constituent_issues": issue_names,
                "constituent_rank": None,
                "gap_type": "UNMAPPED",
                "gap_description": "Haystaq scores not available for mapped columns",
            })
            continue

        avg_score = round(sum(scores) / len(scores), 1)

        # Determine the gap type.
        # Heuristic: if constituent score is high (>60) but council % is low (<5%),
        # it's under-represented. If council % is high (>15%) but score is low (<50),
        # it's over-represented relative to voter priorities.
        if avg_score >= 60 and council_pct < 5:
            gap_type = "UNDER"
            gap_description = f"Voters rank this highly ({avg_score}/100) but council devotes only {council_pct}% of its agenda"
        elif avg_score >= 50 and council_pct < 3:
            gap_type = "UNDER"
            gap_description = f"Moderate voter priority ({avg_score}/100) with minimal council attention ({council_pct}%)"
        elif avg_score < 50 and council_pct > 15:
            gap_type = "OVER"
            gap_description = f"Lower voter priority ({avg_score}/100) but council devotes {council_pct}% of its agenda"
        else:
            gap_type = "MATCH"
            gap_description = f"Voter priority ({avg_score}/100) roughly aligns with council attention ({council_pct}%)"

        results.append({
            "council_topic": topic_name,
            "council_matters": matter_count,
            "council_pct": council_pct,
            "constituent_score": avg_score,
            "constituent_issues": issue_names,
            "constituent_rank": None,  # Filled in below
            "gap_type": gap_type,
            "gap_description": gap_description,
        })

    # Assign constituent ranks based on score (highest score = rank 1).
    # Only rank items that have a score.
    scored = [r for r in results if r["constituent_score"] is not None]
    scored.sort(key=lambda x: x["constituent_score"], reverse=True)
    for i, item in enumerate(scored, 1):
        item["constituent_rank"] = i

    # Sort final results: UNDER gaps first (most actionable), then MATCH, then OVER, then UNMAPPED.
    gap_order = {"UNDER": 0, "OVER": 1, "MATCH": 2, "UNMAPPED": 3}
    results.sort(key=lambda x: (gap_order.get(x["gap_type"], 4), -(x["constituent_score"] or 0)))

    return results


def main():
    """Main function — load data, compute mismatch, save results."""
    print("=" * 60)
    print("07 — Council vs. Constituent Priority Analysis")
    print("=" * 60)
    print()

    # Load inputs.
    print("Loading data...")
    pass1 = load_json(ANALYSIS_DIR / "pass1_legislative_overview.json")
    issue_scores = load_json(CONSTITUENT_DIR / "issue_scores.json")

    if not pass1:
        print("ERROR: pass1_legislative_overview.json not found.")
        print("Run 04_run_analysis.py first.")
        return
    if not issue_scores:
        print("ERROR: issue_scores.json not found.")
        print("Run 06_collect_constituent_data.py first.")
        return

    print(f"  Council topics: {len(pass1.get('topic_areas', []))}")
    print(f"  Total matters: {pass1.get('total_matters', 0)}")
    print(f"  Constituent issues: {len(issue_scores.get('issues', []))}")
    print()

    # Compute the mismatch.
    print("Computing council vs. constituent mismatch...")
    mismatch = build_mismatch_table(pass1, issue_scores)

    # Build the output.
    output = {
        "city": issue_scores.get("city", cfg.db_city_filter),
        "state": issue_scores.get("state", cfg.state_code.upper()),
        "total_council_matters": pass1.get("total_matters", 0),
        "total_voter_count": issue_scores.get("voter_count_with_scores", 0),
        "mismatch_table": mismatch,
        "summary": {
            "under_represented": [m for m in mismatch if m["gap_type"] == "UNDER"],
            "over_represented": [m for m in mismatch if m["gap_type"] == "OVER"],
            "aligned": [m for m in mismatch if m["gap_type"] == "MATCH"],
            "unmapped": [m for m in mismatch if m["gap_type"] == "UNMAPPED"],
        },
    }

    # Save.
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ANALYSIS_DIR / "council_vs_constituent.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    # Print results.
    print()
    print("RESULTS:")
    print("-" * 80)
    print(f"{'Council Topic':<40} {'Matters':>8} {'Pct':>6} {'Voter Score':>12} {'Gap':>8}")
    print("-" * 80)

    for item in mismatch:
        score_str = f"{item['constituent_score']}/100" if item["constituent_score"] else "N/A"
        print(f"{item['council_topic']:<40} {item['council_matters']:>8} {item['council_pct']:>5}% {score_str:>12} {item['gap_type']:>8}")

    print("-" * 80)
    print()

    under = output["summary"]["under_represented"]
    if under:
        print("UNDER-REPRESENTED (voters care, council spends little time):")
        for item in under:
            print(f"  ** {item['council_topic']}: {item['gap_description']}")
    print()

    over = output["summary"]["over_represented"]
    if over:
        print("OVER-REPRESENTED (council spends lots of time, voters less concerned):")
        for item in over:
            print(f"  ** {item['council_topic']}: {item['gap_description']}")
    print()

    print(f"Saved: {out_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
