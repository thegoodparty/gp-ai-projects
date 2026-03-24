"""
02_collect_budget.py — Collect budget and fiscal data from NC LINC/OSBM.

Thin wrapper around the shared LINC budget collector. All collection logic
lives in briefing_poc/collectors/budget_linc.py — this script just bridges
the city config to the collector's config dataclass.

Usage:
    uv run python briefing_poc/charlotte/scripts/02_collect_budget.py
"""

import asyncio

from city_config import cfg
from collectors.budget_linc import BudgetConfig, BudgetDataset, collect_budget


# ============================================================================
# CONFIGURATION
# ============================================================================

DATA_DIR = cfg.data_dir / "budget"


# ============================================================================
# MAIN
# ============================================================================

def main():
    datasets = [
        BudgetDataset(
            dataset_id=ds["dataset_id"],
            description=ds["description"],
            filename=ds["filename"],
        )
        for ds in cfg.budget_datasets
    ]

    config = BudgetConfig(
        api_base_url=cfg.budget_api_base_url,
        municipality=cfg.budget_municipality,
        output_dir=DATA_DIR,
        datasets=datasets,
    )
    asyncio.run(collect_budget(config))


if __name__ == "__main__":
    main()
