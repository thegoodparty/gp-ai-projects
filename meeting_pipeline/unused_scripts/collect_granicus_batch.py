"""
collect_granicus_batch.py — Batch-collect Granicus / Swagit meeting data.

Covers two platform variants:
  - Classic Granicus (RSS): Cibolo TX, Gastonia NC, Greenville NC, Powell OH, Stow OH
  - New Swagit (JSON API):  Beaumont TX, Fairborn OH, Kyle TX, Marysville OH

Usage:
    uv run python meeting_pipeline/scripts/collect_granicus_batch.py
    uv run python meeting_pipeline/scripts/collect_granicus_batch.py --city cibolo-TX
    uv run python meeting_pipeline/scripts/collect_granicus_batch.py --platform classic_granicus
    uv run python meeting_pipeline/scripts/collect_granicus_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from collectors.granicus_scraper import (
    CLASSIC_GRANICUS,
    NEW_SWAGIT,
    GranicusConfig,
    collect_granicus,
)

SOURCES_DIR = _BRIEFING_ROOT / "sources"

# Registry of all Granicus/Swagit cities.
# Keys: city_slug, city, state, platform, subdomain
# Optional: view_id (Classic only), council_keywords (override defaults)
KNOWN_CITIES: list[dict] = [
    # ── Classic Granicus (RSS) ─────────────────────────────────────────────
    {
        "city_slug": "cibolo-TX",
        "city": "Cibolo",
        "state": "TX",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "cibolotx",
        "view_id": 1,
    },
    {
        "city_slug": "gastonia-NC",
        "city": "Gastonia",
        "state": "NC",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "cityofgastonia",
        "view_id": 1,
    },
    {
        "city_slug": "powell-OH",
        "city": "Powell",
        "state": "OH",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "cityofpowell",
        "view_id": 2,
    },
    {
        "city_slug": "greenville-NC",
        "city": "Greenville",
        "state": "NC",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "greenville",
        "view_id": 10,   # view_id=10 = "City Council View". view_id=1 is stale (2022).
    },
    # ── New Swagit (JSON API) ──────────────────────────────────────────────
    {
        "city_slug": "beaumont-TX",
        "city": "Beaumont",
        "state": "TX",
        "platform": NEW_SWAGIT,
        "subdomain": "beaumonttx",
    },
    {
        "city_slug": "fairborn-OH",
        "city": "Fairborn",
        "state": "OH",
        "platform": NEW_SWAGIT,
        "subdomain": "fairbornoh",
    },
    {
        "city_slug": "kyle-TX",
        "city": "Kyle",
        "state": "TX",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "cityofkyletx",
        "view_id": 3,
    },
    {
        "city_slug": "marysville-OH",
        "city": "Marysville",
        "state": "OH",
        "platform": NEW_SWAGIT,
        "subdomain": "marysvilleoh",
    },
    # ── Classic Granicus additions ─────────────────────────────────────────
    {
        "city_slug": "jacksonville-NC",
        "city": "Jacksonville",
        "state": "NC",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "jacksonvillenc",
        "view_id": 2,
    },
    {
        "city_slug": "stow-OH",
        "city": "Stow",
        "state": "OH",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "stowohio",
        "view_id": 1,
    },
    {
        "city_slug": "lago-vista-TX",
        "city": "Lago Vista",
        "state": "TX",
        "platform": CLASSIC_GRANICUS,
        "subdomain": "lagovistatexas",
        "view_id": 1,
    },
]


def find_cities(
    filter_city: str | None = None,
    filter_platform: str | None = None,
) -> list[dict]:
    cities = KNOWN_CITIES
    if filter_city:
        cities = [c for c in cities if c["city_slug"] == filter_city]
    if filter_platform:
        cities = [c for c in cities if c["platform"] == filter_platform]
    return cities


async def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-collect Granicus/Swagit data")
    parser.add_argument("--city", help="Collect only this city slug (e.g. cibolo-TX)")
    parser.add_argument(
        "--platform",
        choices=[CLASSIC_GRANICUS, NEW_SWAGIT],
        help="Collect only this platform variant",
    )
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    parser.add_argument(
        "--lookback", type=int, default=90, help="Days to look back (default: 90)"
    )
    args = parser.parse_args()

    cities = find_cities(filter_city=args.city, filter_platform=args.platform)

    if not cities:
        print("No matching Granicus/Swagit cities found.")
        if args.city:
            print(f"  (filtered by --city {args.city})")
        if args.platform:
            print(f"  (filtered by --platform {args.platform})")
        return

    print(f"Found {len(cities)} Granicus/Swagit cities:")
    for c in cities:
        vid = f" view_id={c['view_id']}" if c["platform"] == CLASSIC_GRANICUS else ""
        print(f"  {c['city_slug']:20s}  platform={c['platform']}{vid}")
    print()

    results = []

    for city_info in cities:
        output_dir = SOURCES_DIR / city_info["city_slug"] / "data" / "granicus"

        config_kwargs: dict = {
            "platform": city_info["platform"],
            "subdomain": city_info["subdomain"],
            "city_name": city_info["city"],
            "output_dir": output_dir,
            "lookback_days": args.lookback,
            "download_pdfs": not args.no_pdfs,
        }
        if "view_id" in city_info:
            config_kwargs["view_id"] = city_info["view_id"]
        if "council_keywords" in city_info:
            config_kwargs["council_keywords"] = city_info["council_keywords"]

        config = GranicusConfig(**config_kwargs)

        print(f"\n{'=' * 60}")
        print(f"Collecting: {city_info['city_slug']} ({city_info['platform']})")
        print(f"{'=' * 60}\n")

        try:
            result = await collect_granicus(config)
            results.append({
                "city": city_info["city_slug"],
                "status": "success",
                "platform": result.platform,
                "total_events": result.total_events,
                "council_events": result.council_events,
                "pdfs": result.pdfs_downloaded,
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
    print(f"\n{'=' * 60}")
    print("GRANICUS BATCH COLLECTION SUMMARY")
    print(f"{'=' * 60}")
    successes = [r for r in results if r["status"] == "success"]
    errors = [r for r in results if r["status"] == "error"]
    print(f"  Successful: {len(successes)}/{len(results)}")
    for r in successes:
        print(
            f"    {r['city']:20s}  {r['council_events']} council events, "
            f"{r['pdfs']} PDFs  ({r['platform']})"
        )
    if errors:
        print(f"  Errors: {len(errors)}")
        for r in errors:
            print(f"    {r['city']}: {r['error']}")

    # Save results
    summary_path = SOURCES_DIR / "granicus_collection_results.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to: {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
