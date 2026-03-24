"""
09_generate_quick_wins.py — Generate concrete, actionable quick wins tied to constituent gaps.

This implements CPO Step 5: "Quick wins with CTAs for polling — what can you do right now?"

The idea: cross-reference the UNDER-represented gaps (from council vs. constituent analysis)
with specific legislative matters, committees, and discussion context to generate targeted,
actionable recommendations a new council member can act on immediately.

No LLM needed — this is pure data joining and cross-referencing.

Example output:
  GAP: Housing and Community Development (voter score 64.2, council 2.3%)
    [HIGH] Join the Housing Council Committee
    [HIGH] Request a staff briefing on Housing and Community Development
    [MEDIUM] Study: Affordable Housing Development Support for Five Points Land Acquisition
    [MEDIUM] Study: Affordable Housing Funding Policy

Prerequisites:
  - data/analysis/council_vs_constituent.json (from script 07)
  - data/analysis/pass1_legislative_overview.json (from script 04)
  - data/analysis/pass2_vote_analysis.json (from script 04)
  - data/analysis/pass5_committee_analysis.json (from script 04)
  - data/discussions/discussion_narratives.json (from script 08, optional)
  - data/legistar/matters.json (from script 01)

Usage:
    python scripts/09_generate_quick_wins.py

Output:
    data/analysis/quick_wins.json
"""

# --- Standard library imports ---
import json
from pathlib import Path
from datetime import date

# --- Project imports ---
from city_config import cfg
from utils import load_json


# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir
ANALYSIS_DIR = DATA_DIR / "analysis"
DISCUSSION_DIR = DATA_DIR / "discussions"
LEGISTAR_DIR = DATA_DIR / "legistar"

# Legistar gateway URL for building links to specific matters.
LEGISTAR_GATEWAY_URL = cfg.legistar_gateway_pattern


# ============================================================================
# HELPERS
# ============================================================================

def build_matter_lookup(matters_path: Path) -> dict:
    """
    Build a lookup from matter title → {id, url, type, status}.

    This lets us enrich the key matter titles from Pass 1 with Legistar URLs
    and metadata, so the quick wins can link directly to official records.
    """
    if not matters_path.exists():
        return {}
    with open(matters_path, "r", encoding="utf-8") as f:
        matters = json.load(f)

    lookup = {}
    for m in matters:
        title = m.get("MatterTitle", "")
        matter_id = m.get("MatterId")
        if title and matter_id:
            lookup[title] = {
                "id": matter_id,
                "url": LEGISTAR_GATEWAY_URL.format(matter_id=matter_id),
                "type": m.get("MatterTypeName", ""),
                "status": m.get("MatterStatusName", ""),
            }
    return lookup


# ============================================================================
# CROSS-REFERENCE FUNCTIONS
# ============================================================================

def find_topic_matters(pass1: dict, topic_name: str) -> list[str]:
    """Find the key matter titles for a given topic area from Pass 1."""
    for topic in pass1.get("topic_areas", []):
        if topic["name"] == topic_name:
            return topic.get("key_matters", [])
    return []


def find_relevant_committee(pass5: dict, topic_name: str) -> dict | None:
    """
    Find the committee most relevant to a topic area.

    Uses keyword matching between the topic name and committee names/roles.
    Returns the committee dict from pass5, or None if no match found.
    """
    # Map topic names to keywords that appear in committee names.
    # Extract keywords from the topic name dynamically (works for any city).
    stop_words = {"and", "the", "of", "for", "in", "on", "a", "an", "to", "with"}
    keywords = [w.lower() for w in topic_name.split() if w.lower() not in stop_words and len(w) > 2]
    if not keywords:
        return None

    for comm in pass5.get("committees", []):
        name_lower = comm["name"].lower()
        for keyword in keywords:
            if keyword in name_lower:
                return comm
    return None


def find_dissent_items_for_topic(pass2: dict, topic_matter_titles: list[str]) -> list[str]:
    """
    Find dissent items that relate to a topic's key matters.

    Cross-references the non-unanimous vote items from Pass 2 with the key
    matter titles from a topic area. Uses substring matching since the LLM
    may have truncated or paraphrased titles.
    """
    topic_titles_lower = {t.lower() for t in topic_matter_titles}

    relevant = []
    for item in pass2.get("dissent_items", []):
        # Dissent items have format "ITEM 47: Title"
        if ": " not in item:
            continue
        title = item.split(": ", 1)[1]
        title_lower = title.lower()

        # Check for substring match in either direction.
        for topic_title in topic_titles_lower:
            if title_lower in topic_title or topic_title in title_lower:
                relevant.append(title)
                break

        # Also check for significant word overlap (3+ shared words).
        if title not in relevant:
            title_words = set(title_lower.split())
            for topic_title in topic_titles_lower:
                topic_words = set(topic_title.split())
                shared = title_words & topic_words
                # Filter out common words.
                shared -= {"the", "a", "an", "of", "for", "and", "to", "in", "on", "at", "by"}
                if len(shared) >= 3:
                    relevant.append(title)
                    break

    return relevant


def find_related_discussions(discussions: dict | None, topic_matter_titles: list[str]) -> list[dict]:
    """
    Find discussion narratives that relate to a topic's key matters.

    Only returns high/medium confidence narratives with verified sources.
    """
    if not discussions:
        return []

    topic_titles_lower = {t.lower() for t in topic_matter_titles}
    related = []

    for narrative in discussions.get("narratives", []):
        if narrative.get("confidence") not in ("high", "medium"):
            continue

        n_title = narrative.get("item_title", "").lower()

        # Check for substring or significant word overlap.
        for topic_title in topic_titles_lower:
            if n_title in topic_title or topic_title in n_title:
                related.append(narrative)
                break
            # Word overlap check.
            n_words = set(n_title.split()) - {"the", "a", "an", "of", "for", "and", "to", "in", "on", "at", "by"}
            t_words = set(topic_title.split()) - {"the", "a", "an", "of", "for", "and", "to", "in", "on", "at", "by"}
            if len(n_words & t_words) >= 3:
                related.append(narrative)
                break

    return related


# ============================================================================
# QUICK WIN GENERATION
# ============================================================================

def generate_quick_wins(
    mismatch: dict,
    pass1: dict,
    pass2: dict,
    pass5: dict,
    matter_lookup: dict,
    discussions: dict | None,
) -> list[dict]:
    """
    Generate concrete quick win recommendations from gap analysis.

    For each UNDER-represented gap, produces a set of specific, actionable
    recommendations tied to real matters, committees, and data.

    Args:
        mismatch: The council_vs_constituent JSON from script 07.
        pass1: The pass1 legislative overview JSON from script 04.
        pass2: The pass2 vote analysis JSON from script 04.
        pass5: The pass5 committee analysis JSON from script 04.
        matter_lookup: Title → {id, url, type, status} from matters.json.
        discussions: The discussion narratives JSON from script 08 (optional).

    Returns:
        A list of gap area dicts, each containing recommended actions.
    """
    quick_wins = []

    # Focus on UNDER-represented gaps — these are the highest-impact opportunities.
    under_gaps = mismatch.get("summary", {}).get("under_represented", [])

    for gap in under_gaps:
        topic_name = gap["council_topic"]
        voter_score = gap["constituent_score"]
        council_pct = gap["council_pct"]
        voter_issues = gap.get("constituent_issues", [])

        # ----- Cross-reference all data sources -----

        # 1. Key matters in this topic area (from Pass 1).
        topic_matter_titles = find_topic_matters(pass1, topic_name)

        # 2. Enrich matters with Legistar URLs and metadata.
        enriched_matters = []
        for title in topic_matter_titles:
            info = matter_lookup.get(title, {})
            enriched_matters.append({
                "title": title,
                "url": info.get("url", ""),
                "type": info.get("type", ""),
                "status": info.get("status", ""),
            })

        # 3. Find the relevant committee.
        committee = find_relevant_committee(pass5, topic_name)

        # 4. Find dissent items related to this topic.
        dissent_items = find_dissent_items_for_topic(pass2, topic_matter_titles)

        # 5. Find discussion narratives.
        related_discussions = find_related_discussions(discussions, topic_matter_titles)

        # ----- Generate actions -----
        actions = []

        # Action 1: Join the relevant committee (HIGH priority).
        if committee:
            actions.append({
                "type": "committee_engagement",
                "priority": "high",
                "title": f"Join or actively participate in the {committee['name']}",
                "rationale": (
                    f"Your constituents rank {', '.join(voter_issues)} as a top priority "
                    f"(score: {voter_score}/100), but the council devotes only {council_pct}% "
                    f"of its agenda to {topic_name}. This committee met "
                    f"{committee.get('meeting_count', 0)} times in the last 6 months and "
                    f"focuses on: {committee.get('recent_focus', 'related topics').rstrip('.')}."
                ),
                "committee_name": committee["name"],
                "committee_meetings": committee.get("meeting_count", 0),
            })

        # Action 2: Request a staff briefing (HIGH priority).
        actions.append({
            "type": "briefing_request",
            "priority": "high",
            "title": f"Request a staff briefing on {topic_name.lower()}",
            "rationale": (
                f"Ask city staff for a comprehensive briefing on the city's current "
                f"approach to {topic_name.lower()}. Focus on: what's pending, "
                f"what's budgeted, and where council action could have the biggest impact. "
                f"This signals your commitment to this constituent priority area."
            ),
        })

        # Action 3: Review specific matters (MEDIUM priority, up to 3).
        for m in enriched_matters[:3]:
            action = {
                "type": "matter_review",
                "priority": "medium",
                "title": f"Study: {m['title']}",
                "rationale": (
                    f"This is a key legislative item in {topic_name}, an area where "
                    f"constituent priorities ({voter_score}/100) outpace council "
                    f"attention ({council_pct}%)."
                ),
                "matter_title": m["title"],
            }
            if m.get("url"):
                action["legistar_url"] = m["url"]
            if m.get("status"):
                action["matter_status"] = m["status"]
            actions.append(action)

        # Action 4: Study past dissent (MEDIUM priority, if relevant).
        if dissent_items:
            actions.append({
                "type": "political_context",
                "priority": "medium",
                "title": f"Study past dissent on {topic_name.lower()} items",
                "rationale": (
                    f"These items had non-unanimous votes, indicating genuine policy "
                    f"disagreement: {'; '.join(dissent_items[:3])}. Understanding why "
                    f"council members disagreed helps you navigate future votes."
                ),
                "dissent_matters": dissent_items[:5],
            })

        # Action 5: Read news coverage (LOW priority, if discussion data available).
        if related_discussions:
            # Extract source references.
            source_refs = []
            for d in related_discussions[:3]:
                for s in d.get("sources", []):
                    if s.get("verified") and s.get("publication") != "Unknown":
                        source_refs.append({
                            "title": s.get("title", ""),
                            "url": s.get("url", ""),
                            "publication": s.get("publication", ""),
                        })
                        break  # One source per narrative is enough.

            actions.append({
                "type": "context_research",
                "priority": "low",
                "title": f"Read news coverage of key {topic_name.lower()} debates",
                "rationale": (
                    f"Recent council debates on {topic_name.lower()} have been covered "
                    f"by local media. Understanding the public narrative helps you "
                    f"engage effectively."
                ),
                "source_references": source_refs,
            })

        # ----- Assemble the gap area -----
        quick_wins.append({
            "gap_topic": topic_name,
            "voter_score": voter_score,
            "council_pct": council_pct,
            "voter_issues": voter_issues,
            "gap_description": gap["gap_description"],
            "committee": committee["name"] if committee else None,
            "key_matters": [m["title"] for m in enriched_matters],
            "contentious_matters": dissent_items,
            "discussion_coverage": len(related_discussions),
            "recommended_actions": actions,
        })

    return quick_wins


# ============================================================================
# MAIN
# ============================================================================

def main():
    """Main function — load data, cross-reference, generate quick wins, save."""
    print("=" * 60)
    print(f"09 — Generate Quick Wins ({cfg.city_name})")
    print("=" * 60)
    print()

    # Load all inputs.
    print("Loading data...")
    mismatch = load_json(ANALYSIS_DIR / "council_vs_constituent.json")
    pass1 = load_json(ANALYSIS_DIR / "pass1_legislative_overview.json")
    pass2 = load_json(ANALYSIS_DIR / "pass2_vote_analysis.json")
    pass5 = load_json(ANALYSIS_DIR / "pass5_committee_analysis.json")
    discussions = load_json(DISCUSSION_DIR / "discussion_narratives.json")

    if not mismatch:
        print("ERROR: council_vs_constituent.json not found. Run 07 first.")
        return
    if not pass1:
        print("ERROR: pass1_legislative_overview.json not found. Run 04 first.")
        return
    if not pass2:
        print("ERROR: pass2_vote_analysis.json not found. Run 04 first.")
        return
    if not pass5:
        print("ERROR: pass5_committee_analysis.json not found. Run 04 first.")
        return

    # Build matter lookup for URLs.
    matter_lookup = build_matter_lookup(LEGISTAR_DIR / "matters.json")
    print(f"  Matter lookup: {len(matter_lookup)} matters")

    under_count = len(mismatch.get("summary", {}).get("under_represented", []))
    print(f"  Under-represented gaps: {under_count}")

    if discussions:
        n_narratives = len(discussions.get("narratives", []))
        print(f"  Discussion narratives: {n_narratives}")
    else:
        print("  Discussion narratives: not available (optional)")

    print()

    # Generate quick wins.
    print("Generating quick wins...")
    quick_wins = generate_quick_wins(mismatch, pass1, pass2, pass5, matter_lookup, discussions)

    # Build output.
    total_actions = sum(len(qw["recommended_actions"]) for qw in quick_wins)
    output = {
        "city": cfg.city_name,
        "generated_date": date.today().isoformat(),
        "total_gap_areas": len(quick_wins),
        "total_recommended_actions": total_actions,
        "data_sources": {
            "mismatch_analysis": "council_vs_constituent.json",
            "legislative_overview": "pass1_legislative_overview.json",
            "vote_analysis": "pass2_vote_analysis.json",
            "committee_analysis": "pass5_committee_analysis.json",
            "discussion_narratives": "discussion_narratives.json" if discussions else None,
            "matter_urls": f"{len(matter_lookup)} matters from Legistar",
        },
        "quick_wins": quick_wins,
    }

    # Save.
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ANALYSIS_DIR / "quick_wins.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # Print results.
    print()
    print("RESULTS:")
    print("-" * 80)

    for qw in quick_wins:
        print(f"\n  GAP: {qw['gap_topic']}")
        print(f"  Voter Score: {qw['voter_score']}/100 | Council: {qw['council_pct']}% of agenda")
        print(f"  Key Matters: {len(qw['key_matters'])} | Contentious: {len(qw['contentious_matters'])} | News Coverage: {qw['discussion_coverage']}")
        print(f"  Committee: {qw['committee'] or 'none found'}")
        print(f"  Recommended Actions ({len(qw['recommended_actions'])}):")
        for action in qw["recommended_actions"]:
            print(f"    [{action['priority'].upper():>6}] {action['title']}")

    print()
    print("-" * 80)
    print(f"Saved: {out_path}")
    print(f"Total: {len(quick_wins)} gap areas, {total_actions} recommended actions")
    print("=" * 60)


if __name__ == "__main__":
    main()
