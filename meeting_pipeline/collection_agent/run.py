"""
run.py — Local CLI entry point for the collection agent.

Usage:
    uv run python -m meeting_pipeline.collection_agent.run --city "Loveland OH"
    uv run python -m meeting_pipeline.collection_agent.run --city "Charlotte NC"
    uv run python -m meeting_pipeline.collection_agent.run --all
    uv run python -m meeting_pipeline.collection_agent.run --discover
    uv run python -m meeting_pipeline.collection_agent.run --discover --re-discover

Full fallback chain per city:
    1. Dedicated platform collector (Legistar, CivicPlus, etc.)
    2. If fail/unknown → misc replay (if nav_config exists)
    3. If fail/no nav_config → misc reason (Playwright + LLM)
    4. If fail → log COLLECTION_FAILED

This file is for local dev/testing only. In production:
    - Lambda: invoke router.route_city() directly as a handler
    - Fargate: same, but with Playwright-enabled container
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent.parent

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage, city_to_slug
from meeting_pipeline.collection_agent.router import route_city
from meeting_pipeline.collection_agent.discovery_agent import run_health_check
from meeting_pipeline.collection_agent import notification_log


def _parse_city_arg(city_arg: str) -> tuple[str, str]:
    """
    Parse "City Name ST" into (city, state).
    Accepts: "Loveland OH", "Canal Winchester OH", "Charlotte NC"
    """
    parts = city_arg.strip().rsplit(" ", 1)
    if len(parts) != 2 or len(parts[1]) != 2:
        raise argparse.ArgumentTypeError(
            f"Invalid city argument '{city_arg}'. Expected format: 'City Name ST' (e.g. 'Loveland OH')"
        )
    return parts[0].strip(), parts[1].upper()


async def run_single_city(city: str, state: str, cfg: AgentConfig) -> None:
    """Run collection for one city and print the result."""
    storage = get_storage(cfg)
    print(f"\n{'='*60}")
    print(f"Collecting: {city}, {state}")
    print(f"{'='*60}")

    event = {"city": city, "state": state}
    result = await route_city(event, storage, cfg)

    print(f"\nResult for {city}, {state}:")
    print(f"  Platform:         {result.platform}")
    print(f"  Events found:     {result.events_found}")
    print(f"  PDFs downloaded:  {result.pdfs_downloaded}")
    print(f"  Requires browser: {result.requires_browser}")
    print(f"  Nav config saved: {result.nav_config_saved}")
    if result.error:
        print(f"  ERROR: {result.error}")


async def run_all_cities(cfg: AgentConfig) -> None:
    """Run collection for all cities in the sources directory."""
    storage = get_storage(cfg)
    all_keys = storage.list_keys(cfg.sources_prefix)

    city_slugs = sorted({
        k.split("/")[-2]    # "meeting_pipeline/sources/{slug}/source.json" → slug
        for k in all_keys
        if k.endswith("source.json") and len(k.split("/")) >= 4
    })

    print(f"\nRunning collection for {len(city_slugs)} cities...")

    results = []
    for slug in city_slugs:
        source_key = f"{cfg.sources_prefix}/{slug}/source.json"
        try:
            source = storage.read_json(source_key)
        except Exception as e:
            print(f"[{slug}] Could not load source.json: {e}")
            continue

        city = source.get("city", slug)
        state = source.get("state", "")
        event = {"city": city, "state": state}

        try:
            result = await route_city(event, storage, cfg)
            results.append(result.to_dict())
            status = "OK" if not result.error else f"FAILED: {result.error}"
            print(f"  [{slug}] {status} — {result.events_found} events, {result.pdfs_downloaded} PDFs")
        except Exception as e:
            print(f"  [{slug}] EXCEPTION: {e}")
            results.append({
                "city": city, "state": state,
                "platform": "unknown", "error": str(e),
            })

    # Save batch results
    output_key = f"{cfg.output_prefix}/collection_results.json"
    storage.write_json(output_key, {"results": results, "total": len(results)})
    print(f"\nResults saved to {output_key}")
    _print_summary(results)


async def run_discover(cfg: AgentConfig, re_discover: bool = False) -> None:
    """Run URL health checks for all cities and detect migrations."""
    storage = get_storage(cfg)
    print(f"\n{'='*60}")
    print("Running URL health checks...")
    print(f"{'='*60}\n")

    results = await run_health_check(
        storage=storage,
        cfg=cfg,
        re_discover=re_discover,
    )

    migrations = [r for r in results if r.migration_detected]
    healthy = [r for r in results if not r.migration_detected and not r.error]
    errored = [r for r in results if r.error and not r.migration_detected]

    print(f"\nHealth Check Summary:")
    print(f"  Total checked:       {len(results)}")
    print(f"  Healthy:             {len(healthy)}")
    print(f"  Migrations detected: {len(migrations)}")
    print(f"  Errors:              {len(errored)}")

    if migrations:
        print(f"\nMigrated cities:")
        for r in migrations:
            print(f"  {r.city}, {r.state}: {r.url}")
            if r.status_code:
                print(f"    Status: {r.status_code}")
            if r.redirected_to:
                print(f"    Redirected to: {r.redirected_to}")

    # Save health check results
    results_data = [r.to_dict() for r in results]
    output_key = f"{cfg.output_prefix}/health_check_results.json"
    storage.write_json(output_key, {"results": results_data})
    print(f"\nResults saved to {output_key}")


def _print_summary(results: list[dict]) -> None:
    succeeded = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]
    total_events = sum(r.get("events_found", 0) for r in succeeded)
    total_pdfs = sum(r.get("pdfs_downloaded", 0) for r in succeeded)

    print(f"\nBatch Summary:")
    print(f"  Cities succeeded: {len(succeeded)}")
    print(f"  Cities failed:    {len(failed)}")
    print(f"  Total events:     {total_events}")
    print(f"  Total PDFs:       {total_pdfs}")

    if failed:
        print(f"\nFailed cities:")
        for r in failed:
            print(f"  {r.get('city')}, {r.get('state')}: {r.get('error')}")


def main():
    parser = argparse.ArgumentParser(
        description="Collection agent — run platform-specific collectors for briefing POC cities",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run one city (dedicated collector or misc fallback):
  uv run python -m meeting_pipeline.collection_agent.run --city "Loveland OH"
  uv run python -m meeting_pipeline.collection_agent.run --city "Charlotte NC"

  # Run all cities:
  uv run python -m meeting_pipeline.collection_agent.run --all

  # Check for URL migrations (health probe only):
  uv run python -m meeting_pipeline.collection_agent.run --discover

  # Check for migrations AND re-run source discovery for affected cities:
  uv run python -m meeting_pipeline.collection_agent.run --discover --re-discover

  # Skip PDF downloads:
  uv run python -m meeting_pipeline.collection_agent.run --city "Durham NC" --no-pdfs

  # Override lookback window:
  uv run python -m meeting_pipeline.collection_agent.run --city "Austin TX" --lookback 180
        """,
    )

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--city", type=str, metavar="'CITY ST'",
                      help="Collect for one city, e.g. 'Loveland OH'")
    mode.add_argument("--all", action="store_true",
                      help="Collect for all cities in sources/")
    mode.add_argument("--discover", action="store_true",
                      help="Run URL health check and detect migrations")

    parser.add_argument("--re-discover", action="store_true",
                        help="With --discover: re-run source discovery for migrated cities")
    parser.add_argument("--no-pdfs", action="store_true",
                        help="Skip PDF downloads (faster, metadata only)")
    parser.add_argument("--lookback", type=int, default=90, metavar="DAYS",
                        help="Lookback window in days (default: 90)")

    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    cfg.lookback_days = args.lookback
    if args.no_pdfs:
        cfg.download_pdfs = False

    if args.city:
        try:
            city, state = _parse_city_arg(args.city)
        except argparse.ArgumentTypeError as e:
            parser.error(str(e))
        asyncio.run(run_single_city(city, state, cfg))

    elif args.all:
        asyncio.run(run_all_cities(cfg))

    elif args.discover:
        asyncio.run(run_discover(cfg, re_discover=args.re_discover))


if __name__ == "__main__":
    main()
