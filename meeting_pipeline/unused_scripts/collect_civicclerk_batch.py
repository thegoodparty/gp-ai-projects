"""
collect_civicclerk_batch.py — Batch-collect CivicClerk OData API data.

Reads discovery source.json files to find CivicClerk cities, then collects
events and downloads agenda PDFs for each.

Usage:
    uv run python meeting_pipeline/scripts/collect_civicclerk_batch.py
    uv run python meeting_pipeline/scripts/collect_civicclerk_batch.py --city fairfield-OH
    uv run python meeting_pipeline/scripts/collect_civicclerk_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.civicclerk import CivicClerkConfig, collect_civicclerk

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# CivicClerk tenants confirmed to have a working OData API.
# Tenant slug -> council category overrides (empty = use defaults).
KNOWN_TENANTS: dict[str, dict] = {
    "fairfieldoh": {"city": "Fairfield", "state": "OH"},
    "midlandtx": {"city": "Midland", "state": "TX"},
    "duncanvilletx": {"city": "Duncanville", "state": "TX"},
    "perrysburgoh": {"city": "Perrysburg", "state": "OH"},
    "shermantx": {"city": "Sherman", "state": "TX"},
    "texarkanatx": {"city": "Texarkana", "state": "TX"},
    "huntersvillenc": {"city": "Huntersville", "state": "NC", "council_categories": ["town board"]},
    "hickorync": {"city": "Hickory", "state": "NC"},
    "westervilleoh": {"city": "Westerville", "state": "OH"},
    "claytonnc": {"city": "Clayton", "state": "NC"},
    "beltontx": {"city": "Belton", "state": "TX"},
    "euclidoh": {"city": "Euclid", "state": "OH"},
    "jacksonvillenc": {"city": "Jacksonville", "state": "NC"},
    "statesvillenc": {"city": "Statesville", "state": "NC"},
    # UXR panel candidates added 2026-04-03
    "windcresttx": {"city": "Windcrest", "state": "TX"},
    "johnstownoh": {"city": "Johnstown", "state": "OH"},
    "brecksvilleoh": {"city": "Brecksville", "state": "OH"},
    "locustnc": {"city": "Locust", "state": "NC"},
    "masonoh": {"city": "Mason", "state": "OH"},
}


def find_civicclerk_cities(filter_city: str | None = None) -> list[dict]:
    """
    Find CivicClerk cities from both discovery source.json files
    and the hardcoded KNOWN_TENANTS registry.
    """
    cities = []
    seen_tenants = set()

    # First: check discovery source.json files
    for city_dir in sorted(SOURCES_DIR.iterdir()):
        source_file = city_dir / "source.json"
        if not source_file.exists():
            continue

        with open(source_file) as f:
            source = json.load(f)

        platform = source.get("best_source", {}).get("platform", "")
        if platform != "civicclerk":
            continue

        city_slug = city_dir.name
        if filter_city and city_slug != filter_city:
            continue

        tenant = source["best_source"].get("config", {}).get("tenant", "")
        if not tenant:
            url = source["best_source"].get("url", "")
            if ".api.civicclerk.com" in url:
                tenant = url.split(".api.civicclerk.com")[0].split("//")[-1]

        if tenant:
            seen_tenants.add(tenant)
            extra = KNOWN_TENANTS.get(tenant, {})
            cities.append({
                "city_slug": city_slug,
                "city": source["city"],
                "state": source["state"],
                "tenant": tenant,
                "council_categories": extra.get("council_categories"),
            })

    # Second: add any KNOWN_TENANTS not found in discovery
    for tenant, info in KNOWN_TENANTS.items():
        if tenant in seen_tenants:
            continue

        city_slug = f"{info['city'].lower().replace(' ', '-')}-{info['state']}"
        if filter_city and city_slug != filter_city:
            continue

        cities.append({
            "city_slug": city_slug,
            "city": info["city"],
            "state": info["state"],
            "tenant": tenant,
            "council_categories": info.get("council_categories"),
        })

    return cities


async def main():
    parser = argparse.ArgumentParser(description="Batch-collect CivicClerk data")
    parser.add_argument("--city", help="Collect only this city slug (e.g. fairfield-OH)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument("--lookback", type=int, default=90, help="Days to look back (default: 90)")
    args = parser.parse_args()

    cities = find_civicclerk_cities(filter_city=args.city)

    if not cities:
        print("No CivicClerk cities found.")
        if args.city:
            print(f"  (filtered by --city {args.city})")
        return

    print(f"Found {len(cities)} CivicClerk cities:")
    for c in cities:
        cats = f" (cats: {c['council_categories']})" if c.get("council_categories") else ""
        print(f"  {c['city_slug']:25s} tenant={c['tenant']}{cats}")
    print()

    results = []

    for city_info in cities:
        output_dir = SOURCES_DIR / city_info["city_slug"] / "data" / "civicclerk"

        config_kwargs = {
            "tenant": city_info["tenant"],
            "city_name": city_info["city"],
            "output_dir": output_dir,
            "lookback_days": args.lookback,
            "download_pdfs": not args.no_pdfs,
        }
        if city_info.get("council_categories"):
            config_kwargs["council_categories"] = city_info["council_categories"]

        config = CivicClerkConfig(**config_kwargs)

        print(f"\n{'='*60}")
        print(f"Collecting: {city_info['city_slug']} (tenant: {city_info['tenant']})")
        print(f"{'='*60}\n")

        try:
            result = await collect_civicclerk(config)
            results.append({
                "city": city_info["city_slug"],
                "status": "success",
                "total_events": result.total_events,
                "council_events": result.council_events,
                "events_with_agenda": result.events_with_agenda,
                "pdfs": result.pdfs_downloaded,
                "categories": result.categories_found,
            })
        except Exception as e:
            print(f"\nERROR collecting {city_info['city_slug']}: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                "city": city_info["city_slug"],
                "status": "error",
                "error": str(e),
            })

    # Summary
    print(f"\n{'='*60}")
    print("CIVICCLERK BATCH COLLECTION SUMMARY")
    print(f"{'='*60}")
    successes = [r for r in results if r["status"] == "success"]
    errors = [r for r in results if r["status"] == "error"]
    print(f"  Successful: {len(successes)}/{len(results)}")
    for r in successes:
        print(f"    {r['city']}: {r['council_events']} council events, {r['events_with_agenda']} with agenda, {r['pdfs']} PDFs")
    if errors:
        print(f"  Errors: {len(errors)}")
        for r in errors:
            print(f"    {r['city']}: {r['error']}")

    # Save results
    summary_path = SOURCES_DIR / "civicclerk_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
