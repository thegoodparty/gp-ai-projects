"""
collect_pilot_batch.py — Collect meeting data for all pilot officials.

Replaces the individual platform batch scripts (collect_civicclerk_batch.py,
collect_civicplus_batch.py, etc.). Uses the collection agent to route each
city to the correct collector automatically based on source.json.

Blocked cities (site down, no collector, city-side API bug) are included so
they appear in the summary log — they will produce COLLECTION_FAILED and move on.

Usage:
    uv run python meeting_pipeline/scripts/collect_pilot_batch.py
    uv run python meeting_pipeline/scripts/collect_pilot_batch.py --city "Durham NC"
    uv run python meeting_pipeline/scripts/collect_pilot_batch.py --no-pdfs
    uv run python meeting_pipeline/scripts/collect_pilot_batch.py --agendas-only
    uv run python meeting_pipeline/scripts/collect_pilot_batch.py --lookback 180
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage, city_to_slug
from meeting_pipeline.collection_agent.router import route_city
from meeting_pipeline.pilot_registry import pilot_cities


# ── Main ──────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace) -> None:
    cfg = AgentConfig.from_env()
    cfg.lookback_days = args.lookback
    cfg.download_pdfs = not args.no_pdfs
    cfg.agendas_only = args.agendas_only
    storage = get_storage(cfg)

    # Filter to a single city if --city was passed
    cities = pilot_cities()
    if args.city:
        parts = args.city.strip().rsplit(" ", 1)
        if len(parts) != 2:
            print(f"ERROR: --city must be in 'City Name ST' format (e.g. 'Durham NC')")
            sys.exit(1)
        city_name, state = parts[0].strip(), parts[1].upper()
        cities = [c for c in cities
                  if c["city"].lower() == city_name.lower() and c["state"] == state]
        if not cities:
            # Not in registry — run ad-hoc for this city
            cities = [{"city": city_name, "state": state}]

    print(f"\n{'='*60}")
    print(f"PILOT COLLECTION — {len(cities)} cities")
    if args.no_pdfs:
        print(f"  (PDF downloads skipped)")
    print(f"{'='*60}\n")

    results = []
    for entry in cities:
        city, state = entry["city"], entry["state"]
        t0 = time.time()
        event = {"city": city, "state": state}

        try:
            result = await route_city(event, storage, cfg)
            elapsed = time.time() - t0
            status = "OK" if not result.error else f"FAILED: {result.error}"
            print(f"  [{city}, {state}] {status} — {result.events_found} events, {result.pdfs_downloaded} PDFs ({elapsed:.1f}s)")
            results.append({
                "city": city, "state": state,
                "platform": result.platform,
                "events": result.events_found,
                "pdfs": result.pdfs_downloaded,
                "error": result.error,
            })
        except Exception as e:
            elapsed = time.time() - t0
            print(f"  [{city}, {state}] EXCEPTION: {e} ({elapsed:.1f}s)")
            results.append({
                "city": city, "state": state,
                "platform": "unknown", "events": 0, "pdfs": 0,
                "error": str(e),
            })

    # Summary
    succeeded = [r for r in results if not r["error"]]
    failed = [r for r in results if r["error"]]

    print(f"\n{'='*60}")
    print(f"SUMMARY")
    print(f"{'='*60}")
    print(f"  Succeeded: {len(succeeded)}/{len(results)}")
    print(f"  Failed:    {len(failed)}/{len(results)}")
    print(f"  Events:    {sum(r['events'] for r in succeeded)}")
    print(f"  PDFs:      {sum(r['pdfs'] for r in succeeded)}")

    if failed:
        print(f"\nFailed cities:")
        for r in failed:
            print(f"  {r['city']}, {r['state']} ({r['platform']}): {r['error']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect meeting data for all pilot officials")
    parser.add_argument("--city", metavar="'CITY ST'",
                        help="Collect one city only, e.g. 'Durham NC'")
    parser.add_argument("--no-pdfs", action="store_true",
                        help="Skip PDF downloads (metadata only)")
    parser.add_argument("--agendas-only", action="store_true",
                        help="Legistar only: skip matter histories/attachments, download agenda PDFs only (much faster)")
    parser.add_argument("--lookback", type=int, default=90, metavar="DAYS",
                        help="Lookback window in days (default: 90)")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
