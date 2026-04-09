"""
collect_haystaq_batch.py — Batch collect Haystaq voter data for all pilot cities.

No per-city config needed. Uses:
  - City name (uppercased) as Databricks filter
  - State code to select the right table
  - Universal Haystaq column set (same across all states)

Usage:
    # Test with 3 cities (one per state):
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/collect_haystaq_batch.py --test

    # Run all cities:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/collect_haystaq_batch.py

    # Run specific city:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/collect_haystaq_batch.py --city cleveland-OH

Storage:
    Writes to STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.
    Output: {sources_prefix}/{city}/constituent/issue_scores.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from shared.databricks_client import DatabricksClient
from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.pilot_registry import PILOT_OFFICIALS, city_slug as make_slug

# ============================================================================
# UNIVERSAL HAYSTAQ COLUMNS (same across all states)
# ============================================================================

ISSUE_SCORES = {
    # Public safety
    "hs_most_important_policy_keep_safe": "Public Safety Priority",
    "hs_violent_crime_very_worried": "Violent Crime Concern",
    "hs_police_trust_yes": "Police Trust",
    # Housing & development
    "hs_affordable_housing_gov_has_role": "Affordable Housing (Gov Role)",
    "hs_gentrification_oppose": "Anti-Gentrification Sentiment",
    # Infrastructure & transportation
    "hs_infrastructure_funding_fund_more": "Infrastructure Funding Support",
    "hs_public_transit_support": "Public Transit Support",
    "hs_gas_tax_support": "Gas Tax Support",
    # Environment
    "hs_most_important_policy_item_environment": "Environment Priority",
    "hs_climate_change_believer": "Climate Change Believer",
    # Education
    "hs_school_funding_more": "School Funding Support",
    "hs_school_choice_support": "School Choice Support",
    "hs_charter_schools_support": "Charter Schools Support",
    "hs_community_college_free_support": "Free Community College Support",
    # Economy & labor
    "hs_most_important_policy_item_economics": "Economic Development Priority",
    "hs_tax_cuts_support": "Tax Cut Support",
    "hs_min_wage_15_increase_support": "Minimum Wage Increase Support",
    "hs_income_inequality_serious": "Income Inequality Concern",
    "hs_unions_beneficial": "Unions Beneficial",
    "hs_econ_anxiety_very_worried": "Economic Anxiety",
    # Public health
    "hs_opioid_crisis_treat": "Opioid: Treatment-first Approach",
    "hs_marijuana_legal_support": "Cannabis Legalization Support",
    # Community priorities
    "hs_most_important_policy_item_help_people": "Helping People Priority",
    "hs_stadium_public_financing_approve": "Stadium Public Financing Support",
    "hs_rank_choice_voting_support": "Ranked Choice Voting Support",
}

CONTEXT_SCORES = {
    "hs_ideology_general_liberal": "Ideology: Liberal",
    "hs_ideology_general_conservative": "Ideology: Conservative",
}

TIER_THRESHOLDS = {"critical": 75, "strong": 60, "moderate": 50}

CATALOG = "goodparty_data_catalog"
SCHEMA = "dbt"


# ============================================================================
# CITY LIST (from pilot_registry)
# ============================================================================

def get_pilot_cities() -> list[dict]:
    """Return deduplicated pilot cities with slug, city, state from registry."""
    seen = set()
    cities = []
    for o in PILOT_OFFICIALS:
        key = (o["city"], o["state"])
        if key in seen:
            continue
        seen.add(key)
        cities.append({
            "slug": make_slug(o["city"], o["state"]),
            "city": o["city"],
            "state": o["state"],
        })
    return cities


def table_names(state_code: str) -> tuple[str, str]:
    """Return (uniform_table, scores_table) for a state."""
    prefix = f"{CATALOG}.{SCHEMA}.stg_dbt_source__l2_s3_{state_code.lower()}"
    return f"{prefix}_uniform", f"{prefix}_haystaq_dna_scores"


# ============================================================================
# QUERY: Issue scores only (fast, ~5s per city)
# ============================================================================

def query_issue_scores(client, city_name: str, state: str) -> dict | None:
    """Query Haystaq issue scores for one city. Returns None on failure."""
    uniform_table, scores_table = table_names(state)
    filter_value = city_name.upper()

    all_scores = {**ISSUE_SCORES, **CONTEXT_SCORES}
    select_parts = ["COUNT(*) as voter_count"]
    for col in all_scores:
        select_parts.append(f"ROUND(AVG(CAST(s.{col} AS DOUBLE)), 1) as {col}")

    select_clause = ",\n        ".join(select_parts)

    query = f"""
        SELECT
            {select_clause}
        FROM {uniform_table} u
        JOIN {scores_table} s
          ON u.LALVOTERID = s.LALVOTERID
        WHERE UPPER(u.Residence_Addresses_City) = "{filter_value}"
    """

    df = client.execute_query(query)

    if df.empty or df.iloc[0]["voter_count"] == 0:
        return None

    row = df.iloc[0].to_dict()

    issues = []
    for col, name in ISSUE_SCORES.items():
        score = float(row[col]) if row[col] is not None else 0.0
        if score >= TIER_THRESHOLDS["critical"]:
            tier, label = 1, "Critical"
        elif score >= TIER_THRESHOLDS["strong"]:
            tier, label = 2, "Strong"
        elif score >= TIER_THRESHOLDS["moderate"]:
            tier, label = 3, "Moderate"
        else:
            tier, label = 4, "Lower"
        issues.append({"column": col, "name": name, "score": score, "tier": tier, "tier_label": label})

    issues.sort(key=lambda x: x["score"], reverse=True)

    context = {}
    for col, name in CONTEXT_SCORES.items():
        score = float(row[col]) if row[col] is not None else 0.0
        context[name] = score

    return {
        "city": filter_value,
        "state": state.upper(),
        "voter_count_with_scores": int(row["voter_count"]),
        "issues": issues,
        "context_scores": context,
        "tier_thresholds": {
            "tier_1_critical": TIER_THRESHOLDS["critical"],
            "tier_2_strong": TIER_THRESHOLDS["strong"],
            "tier_3_moderate": TIER_THRESHOLDS["moderate"],
        },
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Batch collect Haystaq data for pilot cities")
    parser.add_argument("--test", action="store_true", help="Test with 3 cities (one per state)")
    parser.add_argument("--city", type=str, help="Run for a single city slug (e.g. cleveland-OH)")
    parser.add_argument("--dry-run", action="store_true", help="List cities without querying")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    all_cities = get_pilot_cities()

    if args.city:
        cities = [c for c in all_cities if c["slug"] == args.city]
        if not cities:
            print(f"City '{args.city}' not found in pilot registry")
            sys.exit(1)
    elif args.test:
        test_slugs = ["fayetteville-NC", "cleveland-OH", "austin-TX"]
        cities = [c for c in all_cities if c["slug"] in test_slugs]
    else:
        cities = all_cities

    print(f"Batch Haystaq Collection: {len(cities)} cities")
    print(f"States: {sorted(set(c['state'] for c in cities))}")
    print()

    if args.dry_run:
        for c in cities:
            print(f"  {c['slug']:<30} → filter: Residence_Addresses_City = \"{c['city'].upper()}\"")
        return

    print("Connecting to Databricks...")
    client = DatabricksClient()
    if not client.test_connection():
        print("ERROR: Could not connect to Databricks. Check .env credentials.")
        sys.exit(1)
    print("  Connected.\n")

    results = []

    for i, city in enumerate(cities, 1):
        slug = city["slug"]
        name = city["city"]
        state = city["state"]
        print(f"[{i}/{len(cities)}] {name}, {state} (filter: \"{name.upper()}\")...")

        start = time.time()
        try:
            data = query_issue_scores(client, name, state)
            elapsed = time.time() - start

            if data is None:
                print(f"  ⚠ No voters found ({elapsed:.1f}s)")
                results.append({"slug": slug, "status": "no_data", "voters": 0, "time": elapsed})
                continue

            out_key = f"{cfg.sources_prefix}/{slug}/constituent/issue_scores.json"
            storage.write_json(out_key, data)

            voters = data["voter_count_with_scores"]
            top3 = data["issues"][:3]
            print(f"  ✓ {voters:,} voters, top: {top3[0]['name']} ({top3[0]['score']}), "
                  f"{top3[1]['name']} ({top3[1]['score']}), {top3[2]['name']} ({top3[2]['score']}) "
                  f"[{elapsed:.1f}s]")
            results.append({"slug": slug, "status": "ok", "voters": voters, "time": elapsed})

        except Exception as e:
            elapsed = time.time() - start
            print(f"  ✗ Error: {e} [{elapsed:.1f}s]")
            results.append({"slug": slug, "status": "error", "voters": 0, "time": elapsed, "error": str(e)})

    client.close()

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    ok = [r for r in results if r["status"] == "ok"]
    no_data = [r for r in results if r["status"] == "no_data"]
    errors = [r for r in results if r["status"] == "error"]
    total_time = sum(r["time"] for r in results)

    print(f"  Success:  {len(ok)}/{len(results)} cities")
    print(f"  No data:  {len(no_data)} cities")
    print(f"  Errors:   {len(errors)} cities")
    print(f"  Time:     {total_time:.0f}s total, {total_time/len(results):.1f}s avg")

    if ok:
        total_voters = sum(r["voters"] for r in ok)
        print(f"  Voters:   {total_voters:,} total across {len(ok)} cities")

    if no_data:
        print(f"\n  No data found for:")
        for r in no_data:
            print(f"    - {r['slug']}")

    if errors:
        print(f"\n  Errors:")
        for r in errors:
            print(f"    - {r['slug']}: {r.get('error', '?')}")

    print("=" * 70)


if __name__ == "__main__":
    main()
