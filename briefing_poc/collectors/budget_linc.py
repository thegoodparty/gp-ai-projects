"""
budget_linc.py — Reusable NC LINC/OSBM budget data collector.

Fetches fiscal data (revenues, expenditures, property tax rates) from the
NC LINC portal (Opendatasoft API). Works for any NC municipality or county.

Usage:
    from collectors.budget_linc import BudgetConfig, BudgetDataset, collect_budget

    config = BudgetConfig(
        api_base_url="https://linc.osbm.nc.gov/api/explore/v2.1/catalog/datasets",
        municipality="Charlotte",
        output_dir=Path("data/budget"),
        datasets=[BudgetDataset("government", "Fiscal data", "government_fiscal")],
    )
    result = await collect_budget(config)
"""

import asyncio
import json
import csv
from pathlib import Path
from dataclasses import dataclass, field

import httpx


# ============================================================================
# CONFIG AND RESULT DATACLASSES
# ============================================================================

@dataclass
class BudgetDataset:
    """A single LINC dataset to collect."""
    dataset_id: str
    description: str
    filename: str


@dataclass
class BudgetConfig:
    """Configuration for LINC budget data collection."""
    api_base_url: str
    municipality: str
    output_dir: Path
    datasets: list[BudgetDataset] = field(default_factory=list)
    page_size: int = 100


@dataclass
class BudgetResult:
    """Summary of collected budget data."""
    municipality: str = ""
    datasets_collected: int = 0
    total_records: int = 0
    output_dir: Path = field(default_factory=lambda: Path("."))


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _save_json(data, output_dir: Path, filename: str) -> Path:
    """Save data as a JSON file in output_dir."""
    file_path = output_dir / f"{filename}.json"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return file_path


def _save_csv(records: list[dict], output_dir: Path, filename: str) -> Path | None:
    """Save a list of dicts as a CSV file."""
    if not records:
        return None
    file_path = output_dir / f"{filename}.csv"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(records[0].keys())
    with open(file_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)
    return file_path


async def _fetch_dataset(
    client: httpx.AsyncClient,
    base_url: str,
    dataset_id: str,
    municipality: str,
    page_size: int,
) -> list[dict]:
    """Fetch all records for a municipality from a LINC dataset."""
    url = f"{base_url}/{dataset_id}/records"
    params = {
        "where": f"area_name='{municipality}'",
        "limit": page_size,
        "order_by": "year DESC",
    }

    response = await client.get(url, params=params)
    response.raise_for_status()
    data = response.json()

    total_count = data.get("total_count", 0)
    records = data.get("results", [])
    print(f"    Got {len(records)} of {total_count} total records")

    while len(records) < total_count:
        params["offset"] = len(records)
        response = await client.get(url, params=params)
        response.raise_for_status()
        page = response.json().get("results", [])
        if not page:
            break
        records.extend(page)
        print(f"    Got {len(records)} of {total_count} total records")

    return records


# ============================================================================
# MAIN COLLECTION FUNCTION
# ============================================================================

async def collect_budget(config: BudgetConfig) -> BudgetResult:
    """
    Download fiscal datasets for a municipality from NC LINC/OSBM.

    For each dataset in config.datasets:
      1. Fetch all records via the Opendatasoft API (paginated)
      2. Save as JSON
      3. Save as CSV
    """
    config.output_dir.mkdir(parents=True, exist_ok=True)
    total_records = 0

    print(f"Collecting budget data for {config.municipality} from LINC/OSBM...")
    print(f"Saving to: {config.output_dir.resolve()}")
    print()

    async with httpx.AsyncClient(timeout=30) as client:
        for ds in config.datasets:
            print(f"  Fetching: {ds.description}")
            print(f"  Dataset:  {ds.dataset_id}")

            records = await _fetch_dataset(
                client, config.api_base_url, ds.dataset_id,
                config.municipality, config.page_size,
            )

            if not records:
                print(f"    WARNING: No records found for {config.municipality}")
                print()
                continue

            json_path = _save_json(records, config.output_dir, ds.filename)
            print(f"    Saved JSON: {json_path.name}")

            csv_path = _save_csv(records, config.output_dir, ds.filename)
            print(f"    Saved CSV:  {csv_path.name}")

            variables = sorted(set(r.get("variable", "?") for r in records))
            years = sorted(set(r.get("year", "?") for r in records))
            print(f"    Variables ({len(variables)}):")
            for v in variables[:10]:
                print(f"      - {v}")
            if len(variables) > 10:
                print(f"      ... and {len(variables) - 10} more")
            print(f"    Years: {years[0]} to {years[-1]} ({len(years)} years)")
            print()

            total_records += len(records)

    print("=" * 60)
    print("Budget data collection complete!")
    print(f"  Municipality: {config.municipality}")
    print(f"  Datasets:     {len(config.datasets)}")
    print(f"  Saved to:     {config.output_dir.resolve()}")
    print("=" * 60)

    return BudgetResult(
        municipality=config.municipality,
        datasets_collected=len(config.datasets),
        total_records=total_records,
        output_dir=config.output_dir,
    )
