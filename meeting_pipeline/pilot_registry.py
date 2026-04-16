"""
pilot_registry.py — Loads officials and cities from serve_users_unified.csv.

Previously a hardcoded list; now reads from the unified CSV so there is
a single source of truth. All scripts that import PILOT_OFFICIALS or
pilot_cities() continue to work without changes.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

_PIPELINE_DIR = Path(__file__).resolve().parent
_UNIFIED_CSV = _PIPELINE_DIR / "serve_users_unified.csv"


def _load_from_csv() -> list[dict]:
    if not _UNIFIED_CSV.exists():
        return []
    officials = []
    with open(_UNIFIED_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            city = row.get("city", "").strip()
            state = row.get("state", "").strip()
            name = row.get("name", "").strip()
            office = row.get("office", "").strip()
            if not city or not state:
                continue
            officials.append({
                "name": name,
                "city": city,
                "state": state.upper(),
                "role": office or "City Council Member",
            })
    return officials


PILOT_OFFICIALS: list[dict] = _load_from_csv()


def city_slug(city: str, state: str) -> str:
    """Return the filesystem slug for a city, e.g. 'Indian Trail', 'NC' → 'indian-trail-NC'."""
    slug = city.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return f"{slug}-{state.upper()}"


def pilot_cities() -> list[dict]:
    """Return deduplicated list of pilot cities as {city, state} dicts."""
    seen = set()
    cities = []
    for o in PILOT_OFFICIALS:
        key = (o["city"], o["state"])
        if key not in seen:
            seen.add(key)
            cities.append({"city": o["city"], "state": o["state"]})
    return cities
