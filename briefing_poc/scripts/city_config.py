"""
city_config.py — Load city-specific configuration for the POC pipeline.

All city-specific constants (city name, Legistar client ID, LINC URLs,
Haystaq columns, news domains, etc.) live in cities/<city>/city_config.json.
This module loads and validates that file, then provides derived values that
scripts import.

Usage:
    # Run any script with --city to select a city:
    uv run python briefing_poc/scripts/01_collect_legislative.py --city charlotte
    uv run python briefing_poc/scripts/04_run_analysis.py --city wake_county
    uv run python briefing_poc/scripts/01_collect_legislative.py --city raleigh

    # In scripts:
    from city_config import cfg

    print(cfg.city_name)          # "Charlotte"
    print(cfg.data_dir)           # Path(".../cities/charlotte/data")
    print(cfg.legislative_system) # "legistar" or "boarddocs"
"""

import argparse
import json
import sys
from pathlib import Path


# ============================================================================
# PATH SETUP — make briefing_poc/collectors/ importable
# ============================================================================

_BRIEFING_ROOT = Path(__file__).resolve().parent.parent  # scripts/ → briefing_poc/
if str(_BRIEFING_ROOT) not in sys.path:
    sys.path.insert(0, str(_BRIEFING_ROOT))


# ============================================================================
# CLI ARGUMENT PARSING
# ============================================================================

def _parse_city_arg() -> str:
    """Parse --city from argv without interfering with script-specific args."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--city", default="charlotte", help="City config directory name")
    args, _ = parser.parse_known_args()
    return args.city


# ============================================================================
# CONFIG CLASS
# ============================================================================

class CityConfig:
    """Loads city_config.json and provides typed access to all city-specific values."""

    def __init__(self, city_slug: str):
        config_path = _BRIEFING_ROOT / "cities" / city_slug / "city_config.json"
        if not config_path.exists():
            available = [
                d.name for d in (_BRIEFING_ROOT / "cities").iterdir()
                if d.is_dir() and (d / "city_config.json").exists()
            ]
            raise FileNotFoundError(
                f"City config not found: {config_path}\n"
                f"Available cities: {', '.join(sorted(available))}"
            )

        self._config_path = config_path
        with open(config_path, "r", encoding="utf-8") as f:
            self._raw = json.load(f)

        # ── Data directory (per-city) ─────────────────────────────────
        self.data_dir: Path = config_path.parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # ── City identity ──────────────────────────────────────────────
        city = self._raw["city"]
        self.city_name: str = city["name"]                     # "Charlotte"
        self.city_name_full: str = city["name_full"]           # "Charlotte, NC"
        self.city_name_long: str = city["name_long"]           # "Charlotte, North Carolina"
        self.state_code: str = city["state_code"]              # "nc"
        self.state_name: str = city["state_name"]              # "North Carolina"

        # ── Legislative system ────────────────────────────────────────
        leg = self._raw.get("legislative", {})
        self.legislative_system: str = leg.get("system", "legistar")  # "legistar" or "boarddocs"

        # ── Legistar (optional — only for legistar cities) ────────────
        legistar = self._raw.get("legistar", {})
        self.legistar_client: str = legistar.get("client", "")
        self.legistar_base_url: str = (
            f"https://webapi.legistar.com/v1/{self.legistar_client}"
            if self.legistar_client else ""
        )
        self.legistar_gateway_pattern: str = legistar.get("gateway_pattern", "")

        # ── BoardDocs (optional — only for boarddocs cities) ──────────
        boarddocs = self._raw.get("boarddocs", {})
        self.boarddocs_base_url: str = boarddocs.get("base_url", "")
        self.boarddocs_committee_id: str = boarddocs.get("committee_id", "")

        # ── eSCRIBE Meetings (optional — only for escribemeetings cities) ──
        escribemeetings = self._raw.get("escribemeetings", {})
        self.escribemeetings_base_url: str = escribemeetings.get("base_url", "")
        self.escribemeetings_meeting_types: list[str] = escribemeetings.get("meeting_types", [])

        # ── Budget / Fiscal data ───────────────────────────────────────
        budget = self._raw["budget"]
        self.budget_source: str = budget["source"]             # "linc"
        self.budget_api_base_url: str = budget["api_base_url"]
        self.budget_municipality: str = budget["municipality_filter"]
        self.budget_government_url: str = budget["government_url"]
        self.budget_property_tax_url: str = budget["property_tax_url"]
        self.budget_datasets: list[dict] = budget["datasets"]

        # ── Data collection parameters ─────────────────────────────────
        collection = self._raw["data_collection"]
        self.lookback_days: int = collection["lookback_days"]  # 180
        self.data_period: str = collection["data_period"]      # "September 2025 - February 2026"
        self.data_period_display: str = collection["data_period_display"]  # with en-dash

        # ── Entity identity ───────────────────────────────────────────
        entity = self._raw.get("entity", {})
        self.entity_type: str = entity.get("type", "city")
        self.governing_body: str = entity.get("governing_body", "City Council")
        self.governing_body_short: str = entity.get("governing_body_short", "council")
        self.member_title: str = entity.get("member_title", "Council Member")

        # ── Databricks / Haystaq ───────────────────────────────────────
        db = self._raw["databricks"]
        self.db_catalog: str = db["catalog"]                   # "goodparty_data_catalog"
        self.db_schema: str = db["schema"]                     # "dbt"

        # Flexible DB filtering: city-level vs county-level.
        self.db_filter_column: str = db.get("filter_column", "Residence_Addresses_City")
        self.db_filter_value: str = db.get("filter_value", db.get("city_filter", ""))
        self.db_city_filter: str = self.db_filter_value        # backward compat alias

        # Derived table names.
        _prefix = f"{self.db_catalog}.{self.db_schema}.stg_dbt_source__l2_s3_{self.state_code}"
        self.db_uniform_table: str = f"{_prefix}_uniform"
        self.db_scores_table: str = f"{_prefix}_haystaq_dna_scores"
        self.db_flags_table: str = f"{_prefix}_haystaq_dna_flags"

        # Haystaq issue score columns: {column_name: display_name}.
        hs = self._raw["haystaq"]
        self.haystaq_issue_scores: dict[str, str] = hs["issue_scores"]
        self.haystaq_context_scores: dict[str, str] = hs["context_scores"]
        self.haystaq_tier_thresholds: dict[str, int] = hs["tier_thresholds"]

        # ── Discussion narratives ──────────────────────────────────────
        disc = self._raw["discussions"]
        self.discussion_max_items: int = disc["max_items"]     # 20
        self.discussion_news_domains: dict[str, str] = disc["local_news_domains"]
        self.discussion_search_outlets: str = disc["search_focus_outlets"]
        self.discussion_tavily_domains: list[str] = disc.get("tavily_include_domains", [])

        # ── Topic-to-issue mapping (for council vs. constituent analysis) ──
        raw_map = self._raw.get("topic_to_issue_map", {})
        # Filter out the _comment key.
        self.topic_to_issue_map: dict[str, list[str]] = {
            k: v for k, v in raw_map.items() if not k.startswith("_")
        }

    def __repr__(self) -> str:
        return f"CityConfig(city={self.city_name_full!r}, system={self.legislative_system!r})"


# ============================================================================
# SINGLETON — loaded once, imported by all scripts
# ============================================================================

_city_slug = _parse_city_arg()
cfg = CityConfig(_city_slug)
print(f"[config] Loaded: {cfg}")
