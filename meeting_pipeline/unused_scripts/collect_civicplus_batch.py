"""
collect_civicplus_batch.py — Batch-collect CivicPlus AgendaCenter data.

Reads discovery source.json files to find CivicPlus cities, then scrapes
meeting lists and downloads agenda PDFs for each.

Usage:
    uv run python meeting_pipeline/scripts/collect_civicplus_batch.py
    uv run python meeting_pipeline/scripts/collect_civicplus_batch.py --city durham-NC
    uv run python meeting_pipeline/scripts/collect_civicplus_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.civicplus_scraper import CivicPlusConfig, collect_civicplus

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# Known council category IDs (from research + civicplus-scraper-findings.md)
# For cities not in this dict, the scraper will auto-discover.
KNOWN_CATEGORY_IDS: dict[str, int] = {
    "durhamnc.gov": 4,
    "rockymountnc.gov": 5,
}


def find_civicplus_cities(filter_city: str | None = None) -> list[dict]:
    """Read discovery data and return CivicPlus cities."""
    cities = []

    for city_dir in sorted(SOURCES_DIR.iterdir()):
        source_file = city_dir / "source.json"
        if not source_file.exists():
            continue

        with open(source_file) as f:
            source = json.load(f)

        if source.get("best_source", {}).get("platform") != "civicplus":
            continue

        city_slug = city_dir.name
        if filter_city and city_slug != filter_city:
            continue

        # Extract domain from the URL
        url = source["best_source"].get("url", "")
        domain = source["best_source"].get("config", {}).get("domain", "")
        if not domain:
            # Parse from URL: https://durhamnc.gov/AgendaCenter -> durhamnc.gov
            from urllib.parse import urlparse
            parsed = urlparse(url)
            domain = parsed.netloc.replace("www.", "")

        freshness = source.get("best_source", {}).get("freshness", "unknown")

        cities.append({
            "city_slug": city_slug,
            "city": source["city"],
            "state": source["state"],
            "domain": domain,
            "freshness": freshness,
            "most_recent": source["best_source"].get("most_recent_date"),
            "council_category_id": KNOWN_CATEGORY_IDS.get(domain),
        })

    return cities


async def main():
    parser = argparse.ArgumentParser(description="Batch-collect CivicPlus AgendaCenter data")
    parser.add_argument("--city", help="Collect only this city (e.g. durham-NC)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument("--years", help="Comma-separated years (default: current year)")
    args = parser.parse_args()

    cities = find_civicplus_cities(filter_city=args.city)

    if not cities:
        print("No CivicPlus cities found in discovery results.")
        return

    # Filter out stale cities
    fresh_cities = [c for c in cities if c["freshness"] not in ("stale", "stale_warning")]
    stale_cities = [c for c in cities if c["freshness"] in ("stale", "stale_warning")]

    if stale_cities:
        print(f"Skipping {len(stale_cities)} stale cities:")
        for c in stale_cities:
            print(f"  {c['city_slug']} — {c['freshness']} (last update: {c['most_recent']})")
        print()

    print(f"Found {len(fresh_cities)} fresh CivicPlus cities:")
    for c in fresh_cities:
        cat = f"cat={c['council_category_id']}" if c['council_category_id'] else "auto-discover"
        print(f"  {c['city_slug']:25s} {c['domain']:30s} last={c['most_recent']} ({cat})")
    print()

    years = [int(y) for y in args.years.split(",")] if args.years else None  # None = auto-discover
    results = []

    for city_info in fresh_cities:
        output_dir = SOURCES_DIR / city_info["city_slug"] / "data" / "civicplus"

        config = CivicPlusConfig(
            domain=city_info["domain"],
            city_name=city_info["city"],
            council_category_id=city_info["council_category_id"],
            output_dir=output_dir,
            years=years,
            download_pdfs=not args.no_pdfs,
        )

        print(f"\n{'='*60}")
        print(f"Collecting: {city_info['city_slug']}")
        print(f"{'='*60}\n")

        try:
            result = await collect_civicplus(config)
            results.append({
                "city": city_info["city_slug"],
                "status": "success",
                "meetings": result.meetings_found,
                "pdfs": result.pdfs_downloaded,
                "categories": len(result.categories_found),
            })
        except Exception as e:
            print(f"\nERROR collecting {city_info['city_slug']}: {e}")
            results.append({
                "city": city_info["city_slug"],
                "status": "error",
                "error": str(e),
            })

    # Summary
    print(f"\n{'='*60}")
    print("BATCH COLLECTION SUMMARY")
    print(f"{'='*60}")
    successes = [r for r in results if r["status"] == "success"]
    errors = [r for r in results if r["status"] == "error"]
    print(f"  Successful: {len(successes)}/{len(results)}")
    for r in successes:
        print(f"    {r['city']}: {r['meetings']} meetings, {r['pdfs']} PDFs, {r['categories']} categories")
    if errors:
        print(f"  Errors: {len(errors)}")
        for r in errors:
            print(f"    {r['city']}: {r['error']}")

    # Save results
    summary_path = SOURCES_DIR / "civicplus_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
