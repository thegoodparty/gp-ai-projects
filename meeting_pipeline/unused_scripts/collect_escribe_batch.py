"""
collect_escribe_batch.py — Batch-collect eSCRIBE Meetings data.

eSCRIBE is a meeting management platform used by municipalities. It provides
a structured JSON API for fetching past meetings and HTML agenda pages.

Usage:
    uv run python meeting_pipeline/scripts/collect_escribe_batch.py
    uv run python meeting_pipeline/scripts/collect_escribe_batch.py --city marvin-NC
    uv run python meeting_pipeline/scripts/collect_escribe_batch.py --no-pdfs
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from collectors.escribemeetings import EscribeConfig, collect_escribemeetings

SOURCES_DIR = _ROOT / "sources"

KNOWN_CITIES: list[dict] = [
    {
        "city_slug": "marvin-NC",
        "city": "Marvin",
        "state": "NC",
        "base_url": "https://pub-marvinnc.escribemeetings.com",
        "meeting_types": ["Village Council"],
    },
]


def find_escribe_cities(filter_city: str | None = None) -> list[dict]:
    """Return eSCRIBE cities from KNOWN_CITIES, optionally filtered."""
    cities = []
    for entry in KNOWN_CITIES:
        if filter_city and entry["city_slug"] != filter_city:
            continue
        cities.append(entry)
    return cities


async def main_async(args: argparse.Namespace) -> None:
    cities = find_escribe_cities(filter_city=args.city)

    if not cities:
        print(f"No eSCRIBE cities found" + (f" matching --city {args.city}" if args.city else ""))
        return

    print(f"\n{'='*60}")
    print(f"ESCRIBE BATCH COLLECTION — {len(cities)} cities")
    print(f"{'='*60}")

    results = []
    for entry in cities:
        city_slug = entry["city_slug"]
        output_dir = SOURCES_DIR / city_slug / "data" / "escribemeetings"
        output_dir.mkdir(parents=True, exist_ok=True)

        config = EscribeConfig(
            base_url=entry["base_url"],
            city_name=entry["city"],
            output_dir=output_dir,
            meeting_types=entry.get("meeting_types", []),
            lookback_days=180,
        )

        try:
            result = await collect_escribemeetings(config)
            results.append({"city": city_slug, "success": True, "events": result.events_count, "pdfs": result.pdf_count})
        except Exception as e:
            print(f"\n  ✗ Failed for {city_slug}: {e}")
            results.append({"city": city_slug, "success": False, "error": str(e)})

    print(f"\n{'='*60}")
    print(f"ESCRIBE BATCH SUMMARY")
    print(f"{'='*60}")
    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]
    print(f"  Successful: {len(successful)}/{len(results)}")
    for r in successful:
        print(f"    {r['city']:25s}  {r['events']} events, {r['pdfs']} PDFs")
    if failed:
        print(f"  Failed:")
        for r in failed:
            print(f"    {r['city']:25s}  {r['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-collect eSCRIBE meeting data")
    parser.add_argument("--city", help="Only collect this city slug (e.g. marvin-NC)")
    parser.add_argument("--no-pdfs", action="store_true", help="Skip PDF downloads")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
