"""
06_collect_constituent_data.py — Collect constituent voter data from Haystaq via Databricks.

Thin wrapper around the shared Haystaq voter collector. All collection logic
lives in briefing_poc/collectors/haystaq_voter.py — this script just bridges
the city config to the collector's config dataclass.

Prerequisites:
  - Databricks credentials in .env: DATABRICKS_API_KEY, DATABRICKS_SERVER_HOSTNAME, DATABRICKS_HTTP_PATH
  - AWS_PROFILE=work (for Databricks access)

Usage:
    AWS_PROFILE=work uv run python briefing_poc/charlotte/scripts/06_collect_constituent_data.py
"""

import sys

from city_config import cfg
from collectors.haystaq_voter import HaystaqConfig, collect_voter_data


# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir / "constituent"


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 60)
    print(f"06 — Collect Constituent Data for {cfg.city_name} (Haystaq/Databricks)")
    print("=" * 60)
    print()
    print(f"Area: {cfg.db_filter_value}, {cfg.state_code.upper()}")
    print(f"Filter: {cfg.db_filter_column} = {cfg.db_filter_value}")
    print(f"Tables: {cfg.db_uniform_table}")
    print(f"         {cfg.db_scores_table}")
    print()

    config = HaystaqConfig(
        filter_column=cfg.db_filter_column,
        filter_value=cfg.db_filter_value,
        state_code=cfg.state_code,
        uniform_table=cfg.db_uniform_table,
        scores_table=cfg.db_scores_table,
        flags_table=cfg.db_flags_table,
        issue_scores=cfg.haystaq_issue_scores,
        context_scores=cfg.haystaq_context_scores,
        tier_thresholds=cfg.haystaq_tier_thresholds,
        output_dir=DATA_DIR,
    )

    try:
        collect_voter_data(config)
    except ConnectionError as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
