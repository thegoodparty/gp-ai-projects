"""
onboard_cities.py — Onboard new cities into the pipeline.

Runs the one-time setup steps for cities:
  1. Generate manifest.json (expected body from CSV role)
  2. Run source discovery (find URL + platform)
  3. Fetch constituent data from Haystaq (optional)

Usage:
    # Onboard all cities from CSV
    uv run python meeting_pipeline/scripts/onboard_cities.py

    # Onboard a single city
    uv run python meeting_pipeline/scripts/onboard_cities.py --city "Chapel Hill" --state NC

    # Skip constituent data
    uv run python meeting_pipeline/scripts/onboard_cities.py --skip-haystaq

    # Only generate manifests (no discovery or haystaq)
    uv run python meeting_pipeline/scripts/onboard_cities.py --manifests-only

    # Force regenerate manifests even if they exist
    uv run python meeting_pipeline/scripts/onboard_cities.py --force
"""

import argparse
import asyncio
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, get_storage  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Onboard cities into the meeting pipeline")
    parser.add_argument("--city", help="Single city name")
    parser.add_argument("--state", help="State abbreviation (required with --city)")
    parser.add_argument("--csv", help="Alternate CSV file path")
    parser.add_argument("--skip-haystaq", action="store_true", help="Skip constituent data fetch")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip source discovery")
    parser.add_argument("--manifests-only", action="store_true", help="Only generate manifests")
    parser.add_argument("--force", action="store_true", help="Overwrite existing manifests")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    # ── Step 1: Generate manifests ────────────────────────────────────────
    print("=" * 60)
    print("STEP 1: Generate manifests")
    print("=" * 60)

    import meeting_pipeline.scripts.generate_manifests as manifests_module
    from meeting_pipeline.scripts.generate_manifests import (
        build_manifest,
        load_cities_from_csv,
    )

    if args.csv:
        manifests_module._csv_override = Path(args.csv)

    cities = load_cities_from_csv(filter_city=args.city)
    if args.city and args.state:
        cities = [c for c in cities if c["state"] == args.state.upper()]

    print(f"  {len(cities)} cities from CSV\n")

    manifests_created = 0
    manifests_skipped = 0
    for city_info in cities:
        slug = city_info["city_slug"]
        key = f"{cfg.sources_prefix}/{slug}/manifest.json"

        if not args.force and storage.exists(key):
            manifests_skipped += 1
            continue

        if args.dry_run:
            print(f"  [DRY RUN] Would create manifest for {slug}")
            manifests_created += 1
            continue

        manifest = build_manifest(city_info)
        storage.write_json(key, manifest)
        manifests_created += 1
        print(f"  Created: {slug} (body={city_info['expected_body']})")

    print(f"\n  Manifests: {manifests_created} created, {manifests_skipped} skipped")

    if args.manifests_only:
        return

    # ── Step 2: Source discovery ──────────────────────────────────────────
    if not args.skip_discovery:
        print(f"\n{'=' * 60}")
        print("STEP 2: Source discovery")
        print("=" * 60)

        from meeting_pipeline.stages.discover.process import process_one_city

        async def run_discovery():
            import os

            import httpx
            from tavily import TavilyClient

            tavily_key = os.environ.get("TAVILY_API_KEY", "")
            tavily = TavilyClient(api_key=tavily_key) if tavily_key else None

            async with httpx.AsyncClient(
                headers={"User-Agent": "Mozilla/5.0 (compatible; MeetingPipelineBot/1.0)"},
                follow_redirects=True, timeout=20,
            ) as http:
                for i, city_info in enumerate(cities):
                    slug = city_info["city_slug"]
                    source_key = f"{cfg.sources_prefix}/{slug}/source.json"

                    # Skip if already discovered
                    if not args.force and storage.exists(source_key):
                        print(f"  [{i+1}/{len(cities)}] {slug}: exists, skip")
                        continue

                    if args.dry_run:
                        print(f"  [{i+1}/{len(cities)}] {slug}: [DRY RUN] would discover")
                        continue

                    print(f"  [{i+1}/{len(cities)}] {slug}...", end=" ", flush=True)
                    try:
                        result = await process_one_city(
                            city_info["city"], city_info["state"],
                            expected_body=city_info.get("expected_body", ""),
                            tavily_client=tavily, http_client=http,
                        )
                        platform = result.get("best_source", {}).get("platform", "?")
                        storage.write_json(source_key, result)
                        print(f"[{platform}]")
                    except Exception as e:
                        print(f"ERROR: {str(e)[:60]}")

        asyncio.run(run_discovery())

    # ── Step 3: Constituent data ──────────────────────────────────────────
    if not args.skip_haystaq and not args.manifests_only:
        print(f"\n{'=' * 60}")
        print("STEP 3: Constituent data (Haystaq)")
        print("=" * 60)

        try:
            from meeting_pipeline.scripts.collect_haystaq_batch import main as haystaq_main
            if args.dry_run:
                print("  [DRY RUN] Would fetch Haystaq data")
            else:
                # collect_haystaq_batch.main() handles its own CLI args
                haystaq_main()
        except Exception as e:
            print(f"  Haystaq fetch skipped: {e}")

    print(f"\n{'=' * 60}")
    print("ONBOARDING COMPLETE")
    print("=" * 60)


if __name__ == "__main__":
    main()
