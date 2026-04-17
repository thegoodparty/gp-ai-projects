"""
pilot_registry.py — Loads officials and cities from Terry Users2.csv.

Terry Users2.csv is the canonical source of truth for which cities and
officials the pipeline serves. Falls back to serve_users_unified.csv if
Terry Users2.csv is not present (legacy support).

All scripts that import PILOT_OFFICIALS or pilot_cities() continue to work
without changes.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

_PIPELINE_DIR = Path(__file__).resolve().parent
_TERRY_CSV = _PIPELINE_DIR / "Terry Users2.csv"
_UNIFIED_CSV = _PIPELINE_DIR / "serve_users_unified.csv"


def _load_from_csv() -> list[dict]:
    # Prefer Terry Users2.csv; fall back to serve_users_unified.csv
    csv_path = _TERRY_CSV if _TERRY_CSV.exists() else _UNIFIED_CSV
    if not csv_path.exists():
        return []
    officials = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            # Support both Terry (City/State) and unified (city/state) column names
            city = (row.get("City") or row.get("city") or "").strip()
            state = (row.get("State") or row.get("state") or "").strip()
            name = (row.get("Name") or row.get("name") or "").strip()
            office = (row.get("Office") or row.get("office") or "").strip()
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
