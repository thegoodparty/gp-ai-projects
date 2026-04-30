"""
run_pipeline.py — Unified pipeline CLI.

Runs any combination of pipeline stages for any set of cities.
Replaces the old run_serve_users_pipeline.py monolith.

Usage:
    # Full pipeline for all cities
    uv run python meeting_pipeline/scripts/run_pipeline.py

    # Specific phases
    uv run python meeting_pipeline/scripts/run_pipeline.py --phase scan --phase collect

    # Single city
    uv run python meeting_pipeline/scripts/run_pipeline.py --city chapel-hill-NC

    # Multiple cities
    uv run python meeting_pipeline/scripts/run_pipeline.py --city chapel-hill-NC --city austin-TX

    # From CSV
    uv run python meeting_pipeline/scripts/run_pipeline.py --csv meeting_pipeline/serve_users.csv

    # Dry run
    uv run python meeting_pipeline/scripts/run_pipeline.py --phase extract --dry-run

    # Force regenerate
    uv run python meeting_pipeline/scripts/run_pipeline.py --phase briefing --force
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Ensure project root is FIRST on sys.path so top-level `shared/` package
# (llm_gemini, databricks_client) isn't shadowed by meeting_pipeline/shared/.
# Must be index 0 because uv prepends the script directory.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if sys.path[0] != _PROJECT_ROOT:
    sys.path.insert(0, _PROJECT_ROOT)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from meeting_pipeline.shared.config import AgentConfig  # noqa: E402
from meeting_pipeline.stages.orchestrator import ALL_PHASES, run_pipeline  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Run the meeting data pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Phases (run in order):
  discover   Find URL + platform for each city
  scan       Check for upcoming meetings
  collect    Download agenda PDFs
  extract    PDF -> normalized JSON (LLM extraction)
  briefing   Normalized -> briefing (LLM generation)

Examples:
  # Full pipeline for all cities
  %(prog)s

  # Scan + collect only
  %(prog)s --phase scan --phase collect

  # Single city end-to-end
  %(prog)s --city chapel-hill-NC

  # From CSV, extract only
  %(prog)s --csv serve_users.csv --phase extract
""",
    )
    parser.add_argument(
        "--phase", action="append", choices=ALL_PHASES,
        help="Phase(s) to run (default: all). Repeatable.",
    )
    parser.add_argument(
        "--city", action="append", metavar="SLUG",
        help="City slug(s) to process (e.g. chapel-hill-NC). Repeatable.",
    )
    parser.add_argument(
        "--csv", metavar="PATH",
        help="Load cities from CSV file instead of storage",
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()

    asyncio.run(run_pipeline(
        phases=args.phase,
        cfg=cfg,
        city_slugs=args.city,
        csv_path=args.csv,
        force=args.force,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
