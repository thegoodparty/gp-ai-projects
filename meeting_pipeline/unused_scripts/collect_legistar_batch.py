"""
collect_legistar_batch.py — Batch-collect Legistar data for all discovered Legistar cities.

Reads the discovery source.json files to find Legistar cities, then runs the
Legistar collector for each. No city_config.json needed — all config comes from
the discovery output.

Usage:
    uv run python meeting_pipeline/scripts/collect_legistar_batch.py
    uv run python meeting_pipeline/scripts/collect_legistar_batch.py --city austin-TX
    uv run python meeting_pipeline/scripts/collect_legistar_batch.py --lookback 90
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Make meeting_pipeline importable
_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.legistar import LegistarConfig, collect_legistar

# ============================================================================
# DISCOVER LEGISTAR CITIES FROM SOURCE FILES
# ============================================================================

SOURCES_DIR = _BRIEFING_ROOT / "sources"


def find_legistar_cities(filter_city: str | None = None) -> list[dict]:
    """Read all source.json files and return those with platform='legistar'."""
    cities = []

    for city_dir in sorted(SOURCES_DIR.iterdir()):
        source_file = city_dir / "source.json"
        if not source_file.exists():
            continue

        with open(source_file) as f:
            source = json.load(f)

        if source.get("best_source", {}).get("platform") != "legistar":
            continue

        city_slug = city_dir.name  # e.g. "austin-TX"

        if filter_city and city_slug != filter_city:
            continue

        legistar_slug = source["best_source"].get("config", {}).get("legistar_slug", "")
        if not legistar_slug:
            # Try to extract from URL
            url = source["best_source"].get("url", "")
            # URL format: https://webapi.legistar.com/v1/{slug}/events?...
            parts = url.split("/v1/")
            if len(parts) > 1:
                legistar_slug = parts[1].split("/")[0]

        if not legistar_slug:
            print(f"WARNING: No Legistar slug found for {city_slug}, skipping")
            continue

        cities.append({
            "city_slug": city_slug,
            "city": source["city"],
            "state": source["state"],
            "legistar_slug": legistar_slug,
            "most_recent": source["best_source"].get("most_recent_date"),
        })

    return cities


# ============================================================================
# MAIN
# ============================================================================

async def collect_city(city_info: dict, lookback_days: int, output_base: Path) -> dict:
    """Collect Legistar data for a single city."""
    slug = city_info["city_slug"]
    output_dir = output_base / slug / "data" / "legistar"
    output_dir.mkdir(parents=True, exist_ok=True)

    config = LegistarConfig(
        base_url=f"https://webapi.legistar.com/v1/{city_info['legistar_slug']}",
        city_name=city_info["city"],
        output_dir=output_dir,
        lookback_days=lookback_days,
    )

    try:
        result = await collect_legistar(config)
        return {
            "city": slug,
            "status": "success",
            "events": result.events_count,
            "matters": result.matters_count,
            "pdfs": result.pdf_count,
        }
    except Exception as e:
        print(f"\nERROR collecting {slug}: {e}")
        return {
            "city": slug,
            "status": "error",
            "error": str(e),
        }


async def main():
    parser = argparse.ArgumentParser(description="Batch-collect Legistar data")
    parser.add_argument("--city", help="Collect only this city (e.g. austin-TX)")
    parser.add_argument("--lookback", type=int, default=90, help="Days to look back (default: 90)")
    parser.add_argument("--output", default=str(SOURCES_DIR), help="Output base directory")
    args = parser.parse_args()

    cities = find_legistar_cities(filter_city=args.city)

    if not cities:
        print("No Legistar cities found in discovery results.")
        if args.city:
            print(f"  (filtered by --city {args.city})")
        return

    print(f"Found {len(cities)} Legistar cities:")
    for c in cities:
        print(f"  {c['city_slug']} ({c['legistar_slug']}) — last update: {c['most_recent']}")
    print()

    output_base = Path(args.output)
    results = []

    # Run sequentially to be respectful of the Legistar API
    for city_info in cities:
        print(f"\n{'='*60}")
        print(f"Collecting: {city_info['city_slug']}")
        print(f"{'='*60}\n")
        result = await collect_city(city_info, args.lookback, output_base)
        results.append(result)

    # Summary
    print(f"\n{'='*60}")
    print("BATCH COLLECTION SUMMARY")
    print(f"{'='*60}")
    successes = [r for r in results if r["status"] == "success"]
    errors = [r for r in results if r["status"] == "error"]
    print(f"  Successful: {len(successes)}/{len(results)}")
    for r in successes:
        print(f"    {r['city']}: {r['events']} events, {r['matters']} matters, {r['pdfs']} PDFs")
    if errors:
        print(f"  Errors: {len(errors)}")
        for r in errors:
            print(f"    {r['city']}: {r['error']}")

    # Save results summary
    summary_path = output_base / "legistar_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
