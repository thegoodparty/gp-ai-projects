"""
misc/replay.py — Replay mode for misc cities.

Uses a saved nav_config (stored in source.json["best_source"]["nav_config"])
to re-run a proven scraping strategy without LLM analysis.

This is Lambda-safe (no Playwright).
"""

import sys
from datetime import date
from pathlib import Path

# Ensure meeting_pipeline is importable
_BRIEFING_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))

from meeting_pipeline.collectors.generic_html_scraper import GenericScraperConfig, collect_generic
from ..models import CollectionResult, NavConfig
from ..storage import StorageBackend
from ..config import AgentConfig, city_to_slug
from .. import notification_log


class ReplayFailed(Exception):
    """Raised when replay finds no meetings — signals escalation to reason mode."""


async def collect_with_replay(
    event: dict,
    source: dict,
    storage: StorageBackend,
    cfg: AgentConfig,
) -> CollectionResult:
    """
    Replay a previously discovered navigation strategy.

    Args:
        event:   {"city": "...", "state": "..."}
        source:  Parsed source.json dict
        storage: Storage backend for reading/writing
        cfg:     Agent configuration

    Raises:
        ValueError:   if source has no nav_config
        ReplayFailed: if collection returned 0 meetings
    """
    city = event["city"]
    state = event["state"]
    city_slug = city_to_slug(city, state)

    best = source.get("best_source", {})
    nav_raw = best.get("nav_config")

    if not nav_raw:
        raise ValueError(f"No nav_config in source.json for {city}, {state}")

    nav = NavConfig.from_dict(nav_raw)

    # Output goes under cfg.output_prefix / city_slug / "generic"
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    output_dir = repo_root / cfg.output_prefix / city_slug / "generic"

    scraper_cfg = GenericScraperConfig(
        url=nav.entry_url,
        city_name=city,
        output_dir=output_dir,
        strategy=nav.strategy,
        selector=nav.selector,
        keyword_filter=nav.keyword_filter,
        lookback_days=cfg.lookback_days,
        download_pdfs=cfg.download_pdfs,
        follow_url=nav.follow_url,
        verify_ssl=nav.verify_ssl,
    )

    result = await collect_generic(scraper_cfg)

    if result.meetings_found == 0:
        raise ReplayFailed(f"Replay returned 0 meetings for {city}, {state}")

    # Update replay stats in source.json
    nav.replay_success_count += 1
    nav.last_replay_at = date.today().isoformat()
    best["nav_config"] = nav.to_dict()
    source["best_source"] = best

    source_key = f"{cfg.sources_prefix}/{city_slug}/source.json"
    storage.write_json(source_key, source)

    notification_log.log_event(
        notification_log.REPLAY_SUCCESS,
        city, state,
        storage=storage,
        logs_prefix=cfg.logs_prefix,
        meetings_found=result.meetings_found,
        pdfs_downloaded=result.pdfs_downloaded,
        entry_url=nav.entry_url,
        strategy=nav.strategy,
    )

    return CollectionResult(
        city=city,
        state=state,
        platform=nav.platform_guess,
        events_found=result.meetings_found,
        pdfs_downloaded=result.pdfs_downloaded,
        events=[],
        requires_browser=False,
    )
