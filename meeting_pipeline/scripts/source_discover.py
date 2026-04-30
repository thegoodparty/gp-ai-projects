"""
source_discover.py — CLI for running source discovery.

Thin wrapper around stages/discover/process.process_one_city().
Finds the freshest, most active agenda source for each city.

Usage:
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv
    uv run python meeting_pipeline/scripts/source_discover.py --city "Chapel Hill" --state NC
    uv run python meeting_pipeline/scripts/source_discover.py --from-csv --skip-existing
    uv run python meeting_pipeline/scripts/source_discover.py --csv path/to/cities.csv
"""

import argparse
import asyncio
import csv
import sys
import time
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, city_to_slug, get_storage  # noqa: E402
from meeting_pipeline.shared.constants import STATE_ABBREVS  # noqa: E402
from meeting_pipeline.stages.discover.process import process_one_city  # noqa: E402

_PIPELINE_DIR = Path(__file__).resolve().parent.parent
_DEFAULT_CSV = _PIPELINE_DIR / "Terry Users2.csv"


def _load_cities_from_csv(csv_path: Path) -> list[dict]:
    """Load deduplicated cities from a CSV file."""
    seen = set()
    cities = []
    for row in csv.DictReader(csv_path.open()):
        city = (row.get("City") or row.get("city") or "").strip()
        state_raw = (row.get("State") or row.get("state") or row.get("State/Region") or "").strip()
        if not city or not state_raw:
            continue
        state = STATE_ABBREVS.get(state_raw, state_raw.upper()[:2])
        key = (city, state)
        if key in seen:
            continue
        seen.add(key)
        cities.append({"city": city, "state": state})
    return cities


def _should_skip(slug: str, cfg: AgentConfig, storage, skip_existing: bool) -> bool:
    """Check if a city should be skipped based on existing source.json."""
    if not skip_existing:
        return False
    source_key = f"{cfg.sources_prefix}/{slug}/source.json"
    try:
        if not storage.exists(source_key):
            return False
        existing = storage.read_json(source_key)
        bs = existing.get("best_source") or {}
        # Skip manually-set sources
        if bs.get("source") == "manual":
            return True
        freshness = bs.get("freshness", "")
        platform = bs.get("platform", "")
        # Re-run if empty, wrong_entity, stale, or unknown platform
        rerun = {"empty", "wrong_entity", "stale"}
        return freshness not in rerun and platform not in ("unknown", "generic_html", "")
    except Exception:
        return False


async def run_batch(cities: list[dict], cfg: AgentConfig, storage, skip_existing: bool = False):
    """Run discovery for a list of cities sequentially."""
    total_start = time.monotonic()

    print(f"{'='*70}")
    print(f"Source Discover — {len(cities)} cities  (today: {date.today()})")
    print(f"{'='*70}\n")

    results = []
    skipped = 0

    for i, city_info in enumerate(cities):
        city = city_info["city"]
        state = city_info["state"]
        slug = city_to_slug(city, state)

        if _should_skip(slug, cfg, storage, skip_existing):
            src = storage.read_json(f"{cfg.sources_prefix}/{slug}/source.json")
            bs = src.get("best_source", {})
            print(f"  [{i+1}/{len(cities)}] {city:<20s} {state}  [skip] {bs.get('platform','?')}/{bs.get('freshness','?')}")
            skipped += 1
            continue

        try:
            result = await process_one_city(
                city, state, cfg=cfg, storage=storage,
            )
            bs = result.get("best_source") or {}
            platform = bs.get("platform", "?")
            freshness = bs.get("freshness", "?")
            verification = bs.get("verification", {}).get("status", "")
            v_tag = f" [{verification}]" if verification else ""
            print(f"  [{i+1}/{len(cities)}] {city:<20s} {state}  {platform}/{freshness}{v_tag}")
            results.append(result)
        except Exception as e:
            print(f"  [{i+1}/{len(cities)}] {city:<20s} {state}  ERROR: {str(e)[:60]}")
            results.append({"city": city, "state": state, "error": str(e)})

    elapsed = round(time.monotonic() - total_start, 1)

    # Summary
    fresh = sum(1 for r in results if (r.get("best_source") or {}).get("freshness") == "fresh")
    verified = sum(1 for r in results if (r.get("best_source") or {}).get("verification", {}).get("status", "").startswith("verified"))
    errors = sum(1 for r in results if "error" in r)

    print(f"\n{'='*70}")
    print(f"SUMMARY — {elapsed}s total, {len(results)} processed, {skipped} skipped")
    print(f"{'='*70}")
    print(f"  Fresh:    {fresh}")
    print(f"  Verified: {verified}")
    print(f"  Errors:   {errors}")


def main():
    parser = argparse.ArgumentParser(description="Run source discovery for cities")
    parser.add_argument("--city", help="Single city name (e.g. 'Chapel Hill')")
    parser.add_argument("--state", help="State abbreviation (required with --city, or filter --from-csv)")
    parser.add_argument("--from-csv", action="store_true", help="Use default CSV as city list")
    parser.add_argument("--csv", metavar="PATH", help="Alternate CSV file")
    parser.add_argument("--skip-existing", action="store_true", help="Skip cities with working sources")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # Load cities
    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            print(f"ERROR: {csv_path} not found")
            sys.exit(1)
        cities = _load_cities_from_csv(csv_path)
    elif args.from_csv:
        cities = _load_cities_from_csv(_DEFAULT_CSV)
    elif args.city:
        if not args.state:
            print("ERROR: --state required with --city")
            sys.exit(1)
        cities = [{"city": args.city, "state": args.state.upper()}]
    else:
        print("ERROR: specify --city, --from-csv, or --csv")
        sys.exit(1)

    # Filter by state
    if args.state and not args.city:
        cities = [c for c in cities if c["state"].upper() == args.state.upper()]

    if not cities:
        print("No cities to process")
        sys.exit(1)

    asyncio.run(run_batch(cities, cfg, storage, skip_existing=args.skip_existing))


if __name__ == "__main__":
    main()
