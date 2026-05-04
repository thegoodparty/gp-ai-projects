"""
extract_and_normalize.py — Extract agenda items from PDFs and produce normalized meeting JSON.

For each city with upcoming meetings (agenda_posted=true in upcoming_meetings.json):
  1. Find the PDF in storage
  2. Extract text from the PDF (packet preferred over agenda-only)
  3. Use Gemini to extract structured agenda items
  4. Produce normalized meeting JSON with source URLs for QA

Storage:
    Reads/writes via STORAGE_BACKEND (local or s3). Set S3_BUCKET + STORAGE_BACKEND=s3 in .env for S3.
    Output: {output_prefix}/normalized/{city-slug}_{date}.json

Usage:
    uv run python meeting_pipeline/scripts/extract_and_normalize.py
    uv run python meeting_pipeline/scripts/extract_and_normalize.py --dry-run
    uv run python meeting_pipeline/scripts/extract_and_normalize.py --force
    uv run python meeting_pipeline/scripts/extract_and_normalize.py --city chapel-hill-NC
"""

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig  # noqa: E402
from meeting_pipeline.stages.orchestrator import filter_cities, load_cities_from_sources, run_extract  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="Extract agenda items from PDFs")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, no LLM calls")
    parser.add_argument("--force", action="store_true", help="Re-extract even if normalized file exists")
    parser.add_argument("--city", action="append", metavar="SLUG",
                        help="Only process this city slug (repeatable)")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    cities = load_cities_from_sources(cfg)

    if args.city:
        cities = filter_cities(cities, args.city)
        if not cities:
            print(f"No cities found matching: {args.city}")
            sys.exit(1)

    asyncio.run(run_extract(cities, cfg, force=args.force, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
