"""
01_collect_legislative.py — Collect legislative data from the city's system.

Dispatches to the appropriate collector based on the city's legislative system
(configured in city_config.json under "legislative.system"):
  - "legistar"          → collectors/legistar.py          (REST API)
  - "boarddocs"         → collectors/boarddocs.py          (AJAX scraper)
  - "escribemeetings"   → collectors/escribemeetings.py    (JSON API)

All collectors output the same JSON schema (matters.json, events.json, etc.)
so all downstream scripts (02-09) work unchanged regardless of source.

Usage:
    uv run python briefing_poc/scripts/01_collect_legislative.py --city charlotte
    uv run python briefing_poc/scripts/01_collect_legislative.py --city raleigh
"""

import asyncio

from city_config import cfg


# ============================================================================
# CONFIGURATION
# ============================================================================

# Output directory — always "legistar/" for backward compatibility with
# downstream scripts that read from data/legistar/matters.json, etc.
DATA_DIR = cfg.data_dir / "legistar"


# ============================================================================
# MAIN
# ============================================================================

def main():
    system = cfg.legislative_system

    if system == "legistar":
        from collectors.legistar import LegistarConfig, collect_legistar

        config = LegistarConfig(
            base_url=cfg.legistar_base_url,
            city_name=cfg.city_name,
            output_dir=DATA_DIR,
            lookback_days=cfg.lookback_days,
        )
        asyncio.run(collect_legistar(config))

    elif system == "boarddocs":
        from collectors.boarddocs import BoardDocsConfig, collect_boarddocs

        config = BoardDocsConfig(
            base_url=cfg.boarddocs_base_url,
            city_name=cfg.city_name,
            output_dir=DATA_DIR,
            lookback_days=cfg.lookback_days,
            committee_id=cfg.boarddocs_committee_id,
        )
        asyncio.run(collect_boarddocs(config))

    elif system == "escribemeetings":
        from collectors.escribemeetings import EscribeConfig, collect_escribemeetings

        config = EscribeConfig(
            base_url=cfg.escribemeetings_base_url,
            city_name=cfg.city_name,
            output_dir=DATA_DIR,
            meeting_types=cfg.escribemeetings_meeting_types,
            lookback_days=cfg.lookback_days,
        )
        asyncio.run(collect_escribemeetings(config))

    else:
        raise ValueError(
            f"Unknown legislative system: {system!r}. "
            f"Supported: 'legistar', 'boarddocs', 'escribemeetings'"
        )


if __name__ == "__main__":
    main()
