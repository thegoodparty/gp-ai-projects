"""
haystaq_voter.py — Reusable Haystaq/Databricks voter data collector.

Queries GoodParty's Databricks warehouse for Haystaq voter data:
demographics, issue scores, and zip-code breakdowns. Works for any
municipality or county by configuring the filter column and value.

Usage:
    from collectors.haystaq_voter import HaystaqConfig, collect_voter_data

    config = HaystaqConfig(
        filter_column="Residence_Addresses_City",
        filter_value="CHARLOTTE",
        state_code="nc",
        uniform_table="goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_uniform",
        scores_table="goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_haystaq_dna_scores",
        flags_table="goodparty_data_catalog.dbt.stg_dbt_source__l2_s3_nc_haystaq_dna_flags",
        issue_scores={"hs_affordable_housing_gov_has_role": "Affordable Housing", ...},
        context_scores={"hs_ideology": "Ideology", ...},
        tier_thresholds={"critical": 75, "strong": 60, "moderate": 50},
        output_dir=Path("data/constituent"),
    )
    result = collect_voter_data(config)
"""

import json
from pathlib import Path
from dataclasses import dataclass, field


# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class HaystaqConfig:
    """Configuration for Haystaq voter data collection."""
    filter_column: str          # e.g. "Residence_Addresses_City" or "Residence_Addresses_County"
    filter_value: str           # e.g. "CHARLOTTE" or "WAKE" (uppercase)
    state_code: str             # e.g. "nc"
    uniform_table: str
    scores_table: str
    flags_table: str
    issue_scores: dict[str, str]       # {column_name: display_name}
    context_scores: dict[str, str]     # {column_name: display_name}
    tier_thresholds: dict[str, int]    # {"critical": 75, "strong": 60, "moderate": 50}
    output_dir: Path = field(default_factory=lambda: Path("data/constituent"))


@dataclass
class HaystaqResult:
    """Summary of collected voter data."""
    filter_value: str = ""
    total_voters: int = 0
    voters_with_scores: int = 0
    zip_codes: int = 0
    tier_1_issues: int = 0
    tier_2_issues: int = 0
    output_dir: Path = field(default_factory=lambda: Path("."))


# ============================================================================
# QUERY FUNCTIONS
# ============================================================================

def _query_demographics(client, config: HaystaqConfig) -> dict:
    """Query voter demographics using configurable filter column/value."""
    print(f"  Querying demographics for {config.filter_value}...")

    query = f"""
        SELECT
            COUNT(*) as total_voters,
            COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Democrat%" THEN LALVOTERID END) as democrats,
            COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Republican%" THEN LALVOTERID END) as republicans,
            COUNT(DISTINCT CASE WHEN Parties_Description LIKE "%Unaffiliated%"
                               OR Parties_Description LIKE "%Independent%" THEN LALVOTERID END) as independents,
            ROUND(AVG(CAST(Voters_Age AS INT)), 1) as avg_age,
            COUNT(DISTINCT CASE WHEN Voters_Gender = "M" THEN LALVOTERID END) as male,
            COUNT(DISTINCT CASE WHEN Voters_Gender = "F" THEN LALVOTERID END) as female
        FROM {config.uniform_table}
        WHERE UPPER({config.filter_column}) = "{config.filter_value}"
    """

    df = client.execute_query(query)

    if df.empty:
        print(f"  WARNING: No voters found for {config.filter_value}")
        return {}

    row = df.iloc[0].to_dict()

    result = {
        "area": config.filter_value,
        "state": config.state_code.upper(),
        "total_voters": int(row["total_voters"]),
        "party_breakdown": {
            "democrat": int(row["democrats"]),
            "republican": int(row["republicans"]),
            "independent_unaffiliated": int(row["independents"]),
            "other": int(row["total_voters"] - row["democrats"] - row["republicans"] - row["independents"]),
        },
        "avg_age": float(row["avg_age"]) if row["avg_age"] is not None else None,
        "gender": {
            "male": int(row["male"]),
            "female": int(row["female"]),
            "other_unknown": int(row["total_voters"] - row["male"] - row["female"]),
        },
    }

    print(f"    Total voters: {result['total_voters']:,}")
    print(f"    Avg age: {result['avg_age']}")
    print(f"    D/R/I: {result['party_breakdown']['democrat']:,} / "
          f"{result['party_breakdown']['republican']:,} / "
          f"{result['party_breakdown']['independent_unaffiliated']:,}")

    return result


def _query_issue_scores(client, config: HaystaqConfig) -> dict:
    """Query average Haystaq issue scores using configurable filter."""
    print(f"  Querying issue scores for {config.filter_value}...")

    all_scores = {**config.issue_scores, **config.context_scores}
    select_parts = ["COUNT(*) as voter_count"]
    for col_name in all_scores:
        select_parts.append(f"ROUND(AVG(CAST(s.{col_name} AS DOUBLE)), 1) as {col_name}")

    select_clause = ",\n        ".join(select_parts)

    query = f"""
        SELECT
            {select_clause}
        FROM {config.uniform_table} u
        JOIN {config.scores_table} s
          ON u.LALVOTERID = s.LALVOTERID
        WHERE UPPER(u.{config.filter_column}) = "{config.filter_value}"
    """

    df = client.execute_query(query)

    if df.empty:
        print(f"  WARNING: No Haystaq scores found for {config.filter_value}")
        return {}

    row = df.iloc[0].to_dict()

    tier_1_threshold = config.tier_thresholds["critical"]
    tier_2_threshold = config.tier_thresholds["strong"]
    tier_3_threshold = config.tier_thresholds["moderate"]

    issues = []
    for col_name, display_name in config.issue_scores.items():
        score = float(row[col_name]) if row[col_name] is not None else 0.0

        if score >= tier_1_threshold:
            tier, tier_label = 1, "Critical"
        elif score >= tier_2_threshold:
            tier, tier_label = 2, "Strong"
        elif score >= tier_3_threshold:
            tier, tier_label = 3, "Moderate"
        else:
            tier, tier_label = 4, "Lower"

        issues.append({
            "column": col_name,
            "name": display_name,
            "score": score,
            "tier": tier,
            "tier_label": tier_label,
        })

    issues.sort(key=lambda x: x["score"], reverse=True)

    context = {}
    for col_name, display_name in config.context_scores.items():
        score = float(row[col_name]) if row[col_name] is not None else 0.0
        context[display_name] = score

    result = {
        "area": config.filter_value,
        "state": config.state_code.upper(),
        "voter_count_with_scores": int(row["voter_count"]),
        "issues": issues,
        "context_scores": context,
        "tier_thresholds": {
            "tier_1_critical": tier_1_threshold,
            "tier_2_strong": tier_2_threshold,
            "tier_3_moderate": tier_3_threshold,
        },
    }

    print(f"    Voters with Haystaq scores: {result['voter_count_with_scores']:,}")
    print(f"    Top 3 issues:")
    for issue in issues[:3]:
        print(f"      {issue['name']}: {issue['score']} (Tier {issue['tier']} — {issue['tier_label']})")

    return result


def _query_zip_breakdown(client, config: HaystaqConfig) -> dict:
    """Query issue scores broken down by zip code."""
    print(f"  Querying zip code breakdown for {config.filter_value}...")

    # Use a subset of issue scores for zip-level breakdown.
    zip_score_cols = [col for col in config.issue_scores.keys()][:7]

    score_selects = []
    for col in zip_score_cols:
        score_selects.append(f"ROUND(AVG(CAST(s.{col} AS DOUBLE)), 1) as {col}")

    score_clause = ",\n            ".join(score_selects)

    query = f"""
        SELECT
            u.Residence_Addresses_Zip as zip_code,
            COUNT(*) as voter_count,
            {score_clause}
        FROM {config.uniform_table} u
        JOIN {config.scores_table} s
          ON u.LALVOTERID = s.LALVOTERID
        WHERE UPPER(u.{config.filter_column}) = "{config.filter_value}"
        GROUP BY u.Residence_Addresses_Zip
        ORDER BY voter_count DESC
    """

    df = client.execute_query(query)

    if df.empty:
        print(f"  WARNING: No zip code data found for {config.filter_value}")
        return {}

    zip_data = []
    for _, row in df.iterrows():
        zip_entry = {
            "zip_code": str(row["zip_code"]),
            "voter_count": int(row["voter_count"]),
            "scores": {},
        }
        for col in zip_score_cols:
            display_name = config.issue_scores.get(col, col)
            score = float(row[col]) if row[col] is not None else 0.0
            zip_entry["scores"][display_name] = score
        zip_data.append(zip_entry)

    result = {
        "area": config.filter_value,
        "state": config.state_code.upper(),
        "total_zip_codes": len(zip_data),
        "zip_codes": zip_data,
    }

    print(f"    Zip codes found: {result['total_zip_codes']}")
    if zip_data:
        top = zip_data[0]
        print(f"    Largest zip: {top['zip_code']} ({top['voter_count']:,} voters)")

    return result


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

def collect_voter_data(config: HaystaqConfig, client=None) -> HaystaqResult:
    """
    Run all three Haystaq queries and save results.

    Args:
        config: Collection configuration.
        client: An active DatabricksClient. If None, creates one.

    Returns:
        HaystaqResult summary.
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)

    # Create client if not provided.
    owns_client = False
    if client is None:
        from shared.databricks_client import DatabricksClient
        print("Connecting to Databricks...")
        client = DatabricksClient()
        if not client.test_connection():
            raise ConnectionError("Could not connect to Databricks. Check .env credentials.")
        print("  Connected successfully")
        print()
        owns_client = True

    try:
        # Query 1: Demographics
        print("Step 1/3: Voter Demographics")
        demographics = _query_demographics(client, config)
        if demographics:
            with open(config.output_dir / "demographics.json", "w", encoding="utf-8") as f:
                json.dump(demographics, f, indent=2)
            print(f"  Saved: demographics.json")
        print()

        # Query 2: Issue Scores
        print("Step 2/3: Issue Priority Scores")
        issue_scores = _query_issue_scores(client, config)
        if issue_scores:
            with open(config.output_dir / "issue_scores.json", "w", encoding="utf-8") as f:
                json.dump(issue_scores, f, indent=2)
            print(f"  Saved: issue_scores.json")
        print()

        # Query 3: Zip Code Breakdown
        print("Step 3/3: Zip Code Breakdown")
        zip_breakdown = _query_zip_breakdown(client, config)
        if zip_breakdown:
            with open(config.output_dir / "zip_breakdown.json", "w", encoding="utf-8") as f:
                json.dump(zip_breakdown, f, indent=2)
            print(f"  Saved: zip_breakdown.json")
        print()

        # Combined Summary
        print("Building combined summary...")
        summary = {
            "area": config.filter_value,
            "state": config.state_code.upper(),
            "demographics": demographics,
            "issue_scores": issue_scores,
            "zip_breakdown": zip_breakdown,
        }
        with open(config.output_dir / "constituent_summary.json", "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2)
        print(f"  Saved: constituent_summary.json")
        print()

        # Build result
        total_voters = demographics.get("total_voters", 0) if demographics else 0
        voters_with_scores = issue_scores.get("voter_count_with_scores", 0) if issue_scores else 0
        zip_count = zip_breakdown.get("total_zip_codes", 0) if zip_breakdown else 0
        tier_1 = len([i for i in issue_scores.get("issues", []) if i["tier"] == 1]) if issue_scores else 0
        tier_2 = len([i for i in issue_scores.get("issues", []) if i["tier"] == 2]) if issue_scores else 0

        print("=" * 60)
        print("COLLECTION COMPLETE")
        print("=" * 60)
        print(f"  Total voters: {total_voters:,}")
        print(f"  Tier 1 issues (critical): {tier_1}")
        print(f"  Tier 2 issues (strong): {tier_2}")
        print(f"  Zip codes: {zip_count}")
        print(f"  Output: {config.output_dir}/")
        print("=" * 60)

        return HaystaqResult(
            filter_value=config.filter_value,
            total_voters=total_voters,
            voters_with_scores=voters_with_scores,
            zip_codes=zip_count,
            tier_1_issues=tier_1,
            tier_2_issues=tier_2,
            output_dir=config.output_dir,
        )

    finally:
        if owns_client:
            client.close()
