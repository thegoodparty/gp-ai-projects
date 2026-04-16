"""
rerank_sources.py — Re-rank all_candidates in source.json using the fixed PLATFORM_TIER
scoring and update best_source for any city where the winner changes.

This repairs the regression where unsupported platforms (granicus, municode, etc.)
were incorrectly ranked above supported ones (civicclerk, civicplus) due to wrong
PLATFORM_TIER values. No web calls needed — uses existing all_candidates data.

Usage:
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/rerank_sources.py --dry-run
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/rerank_sources.py
    AWS_PROFILE=goodparty uv run python meeting_pipeline/scripts/rerank_sources.py --slugs imperial-CA rogers-AR
"""

import argparse
import csv
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
for p in [str(_ROOT), str(_PROJECT_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from meeting_pipeline.collection_agent.config import AgentConfig, get_storage
from meeting_pipeline.scripts.source_discover import candidate_score, rank_candidates

SUPPORTED_PLATFORMS = {"legistar", "civicplus", "civicclerk", "boarddocs", "escribe"}

# Used to gate upgrades — only apply if new platform tier > old platform tier
from meeting_pipeline.scripts.source_discover import PLATFORM_TIER as _PLATFORM_TIER
TERRY_CSV = _ROOT / "Terry Users2.csv"


def load_terry_slugs() -> list[str]:
    """Load all city slugs from Terry Users2.csv."""
    from meeting_pipeline.collection_agent.config import city_to_slug
    slugs = []
    with open(TERRY_CSV) as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = row.get("City", "").strip()
            state = row.get("State", "").strip()
            if city and state:
                slugs.append(city_to_slug(city, state))
    return list(dict.fromkeys(slugs))  # deduplicate, preserve order


def rebuild_best_source(source_data: dict) -> dict | None:
    """
    Re-rank all_candidates and return the new best_source dict, or None if unchanged.
    """
    city = source_data.get("city", "")
    state = source_data.get("state", "")
    candidates = source_data.get("all_candidates", [])
    if not candidates:
        return None

    ranked = rank_candidates(candidates, city, state)

    old_best = source_data.get("best_source", {})
    new_winner = ranked[0]

    old_platform = old_best.get("platform", "unknown")
    new_platform = new_winner.get("platform", "unknown")
    old_url = old_best.get("url", "")
    new_url = new_winner.get("url", "")

    if old_url == new_url:
        return None  # No change

    # Only upgrade — never downgrade or make lateral moves.
    # If the new winner's platform tier is not strictly higher than the old one,
    # skip — this prevents blog posts / news articles / homepages from displacing
    # a working supported platform just because they happen to be "fresh".
    old_tier = _PLATFORM_TIER.get(old_platform, 4)
    new_tier = _PLATFORM_TIER.get(new_platform, 4)
    if new_tier <= old_tier:
        return None  # Not an upgrade — skip

    # Build a new best_source by merging the winner candidate with any existing best_source config
    new_best = dict(old_best)  # preserve config, collection_method, etc.
    new_best.update({
        "platform": new_platform,
        "url": new_url,
        "display_url": new_winner.get("display_url", new_url),
        "freshness": new_winner.get("freshness", "unknown"),
        "most_recent_date": new_winner.get("most_recent_date"),
        "days_since_update": new_winner.get("days_since_update"),
        "date_source": new_winner.get("date_source"),
        "collection_method": new_winner.get("collection_method", "html_scrape_pdf"),
        "notes": new_winner.get("notes", ""),
    })
    # Don't carry over config from old best_source unless platforms match
    if old_platform != new_platform:
        new_best.pop("config", None)
        new_best["config"] = new_winner.get("config", {})

    return new_best


def main():
    parser = argparse.ArgumentParser(description="Re-rank source.json candidates with fixed scoring")
    parser.add_argument("--dry-run", action="store_true", help="Print changes without writing to S3")
    parser.add_argument("--slugs", nargs="*", help="Specific slugs to process (default: all Terry cities)")
    args = parser.parse_args()

    cfg = AgentConfig.from_env()
    storage = get_storage(cfg)

    if args.slugs:
        slugs = args.slugs
    else:
        slugs = load_terry_slugs()
        print(f"Loaded {len(slugs)} slugs from Terry Users2.csv")

    changed = []
    unchanged = []
    missing = []

    for slug in slugs:
        key = f"{cfg.sources_prefix}/{slug}/source.json"
        if not storage.exists(key):
            missing.append(slug)
            continue

        try:
            source = storage.read_json(key)
        except Exception as e:
            print(f"  {slug}: ERROR reading — {e}")
            continue

        new_best = rebuild_best_source(source)
        if new_best is None:
            unchanged.append(slug)
            continue

        old_best = source.get("best_source", {})
        old_platform = old_best.get("platform", "unknown")
        new_platform = new_best.get("platform", "unknown")
        old_url = old_best.get("url", "")
        new_url = new_best.get("url", "")

        print(
            f"  {slug:<40} {old_platform} → {new_platform}\n"
            f"    OLD: {old_url}\n"
            f"    NEW: {new_url}"
        )
        changed.append(slug)

        if not args.dry_run:
            source["best_source"] = new_best
            # Update rank numbers in all_candidates to reflect new ordering
            city = source.get("city", "")
            state = source.get("state", "")
            source["all_candidates"] = rank_candidates(source.get("all_candidates", []), city, state)
            storage.write_json(key, source)
            print(f"    ✓ Updated S3")

    print(f"\n{'DRY RUN — ' if args.dry_run else ''}Summary:")
    print(f"  Changed:   {len(changed)}")
    print(f"  Unchanged: {len(unchanged)}")
    print(f"  Missing:   {len(missing)}")

    if changed:
        print(f"\nCities needing re-scan:")
        for s in changed:
            print(f"  {s}")


if __name__ == "__main__":
    main()
