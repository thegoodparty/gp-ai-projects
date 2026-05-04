"""
generate_briefing.py — Generate council meeting briefings from normalized meeting data.

Takes normalized meeting JSON from output/normalized/ and produces a full briefing
JSON with executive summary, priority issue cards, detail pages, and constituent data.

Pipeline:
  Pass 1: Categorize all agenda items + identify priority issues
  Pass 2: Generate card content for each priority issue
  Pass 3: Generate detail content for each priority issue

Storage:
    Reads/writes via STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.

Usage:
    # Generate for a specific normalized meeting
    uv run python meeting_pipeline/scripts/generate_briefing.py --file output/normalized/chapel-hill-NC_2026-04-15.json

    # Generate for a city (most recent normalized meeting)
    uv run python meeting_pipeline/scripts/generate_briefing.py --city chapel-hill-NC

    # Generate for all normalized meetings
    uv run python meeting_pipeline/scripts/generate_briefing.py --batch

    # Force regenerate
    uv run python meeting_pipeline/scripts/generate_briefing.py --batch --force
"""

import argparse
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig, get_storage  # noqa: E402
from meeting_pipeline.stages.briefing.generate import generate_briefing_for_meeting  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Generate council meeting briefings")
    parser.add_argument("--file", help="Storage key for a normalized meeting JSON")
    parser.add_argument("--city", action="append", metavar="SLUG",
                        help="City slug(s). With --batch, filters to these cities. Without --batch, uses most recent.")
    parser.add_argument("--batch", action="store_true", help="Generate for all normalized meetings")
    parser.add_argument("--from-date", help="Only meetings on or after this date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--force", action="store_true", help="Regenerate even if briefing exists")
    args = parser.parse_args()

    if not args.file and not args.city and not args.batch:
        parser.error("Specify --file, --city, or --batch")

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)
    normalized_prefix = f"{cfg.output_prefix}/normalized"

    if args.file:
        target_keys = [args.file]
    elif args.city and not args.batch:
        all_keys = storage.list_keys(normalized_prefix)
        city_slug = args.city[0].lower().replace(" ", "-")
        matches = sorted(
            k for k in all_keys
            if k.split("/")[-1].lower().startswith(city_slug) and k.endswith(".json")
        )
        if not matches:
            print(f"No normalized files found for city: {args.city[0]}")
            sys.exit(1)
        target_keys = [matches[-1]]
    else:
        all_keys = storage.list_keys(normalized_prefix)
        target_keys = sorted(
            k for k in all_keys
            if re.search(r"[^/]+_\d{4}-\d{2}-\d{2}\.json$", k)
        )
        if args.city:
            city_filter = {s.lower().replace(" ", "-") for s in args.city}
            before = len(target_keys)
            target_keys = [
                k for k in target_keys
                if any(k.split("/")[-1].lower().startswith(slug) for slug in city_filter)
            ]
            print(f"City filter {sorted(city_filter)}: {len(target_keys)} of {before} files")

        if args.from_date:
            before = len(target_keys)
            target_keys = [
                k for k in target_keys
                if (m := re.search(r"(\d{4}-\d{2}-\d{2})\.json$", k)) and m.group(1) >= args.from_date
            ]
            print(f"Date filter >= {args.from_date}: {len(target_keys)} of {before} files")

        if not args.force:
            briefing_prefix = f"{cfg.output_prefix}/briefings"
            existing = set(storage.list_keys(briefing_prefix))
            before = len(target_keys)
            target_keys = [
                k for k in target_keys
                if not any(k.split("/")[-1][:-5] in bk for bk in existing)
            ]
            skipped = before - len(target_keys)
            if skipped:
                print(f"Skipping {skipped} with existing briefings (--force to regenerate)")

        if not target_keys:
            print("No normalized meeting files to process")
            sys.exit(1)

    results = []
    for key in target_keys:
        filename = key.split("/")[-1]
        print(f"\nProcessing: {filename}")
        try:
            result = generate_briefing_for_meeting(key, storage, cfg, dry_run=args.dry_run)
        except Exception as e:
            print(f"  Failed: {e}")
            result = {"status": "error", "error": str(e), "cost": 0}
        result["file"] = filename
        results.append(result)

    if len(results) > 1:
        print(f"\n{'=' * 60}")
        print("BRIEFING SUMMARY")
        print(f"{'=' * 60}")
        ok = sum(1 for r in results if r.get("status") == "ok")
        errs = sum(1 for r in results if r.get("status") == "error")
        cost = sum(r.get("cost", 0) for r in results)
        print(f"  Generated: {ok}/{len(results)}, errors: {errs}, cost: ${cost:.4f}")


if __name__ == "__main__":
    main()
