"""
pilot_registry.py — Single source of truth for pilot officials and cities.

Used by:
  - scripts/collect_pilot_batch.py   (collection)
  - scripts/collect_haystaq_batch.py (voter data)
  - scripts/generate_meeting_queue.py (queue building)

To add a new official: add an entry to PILOT_OFFICIALS.
To remove one: delete their entry.
Everything else picks up the change automatically.
"""

from __future__ import annotations


PILOT_OFFICIALS: list[dict] = [
    # ── Tier 1: CivicClerk / Legistar ────────────────────────────────────────
    {"name": "Nicole Shook",         "city": "Johnstown",     "state": "OH", "role": "City Council Member"},
    {"name": "AJ Ganim",             "city": "Brecksville",   "state": "OH", "role": "City Council Member"},
    {"name": "Mike Haigler",         "city": "Locust",        "state": "NC", "role": "City Council Member"},
    {"name": "Jay Davis",            "city": "Texarkana",     "state": "TX", "role": "City Council Member"},
    {"name": "Dan Reese",            "city": "Windcrest",     "state": "TX", "role": "City Council Member"},
    {"name": "Mickey Smith",         "city": "Jacksonville",  "state": "NC", "role": "City Council Member"},
    {"name": "Kim Singh",            "city": "Mason",         "state": "OH", "role": "City Council Member"},
    {"name": "Doug Weiss",           "city": "Pflugerville",  "state": "TX", "role": "City Council Member"},

    # ── Tier 2: CivicPlus / Granicus ─────────────────────────────────────────
    {"name": "Guy Guidone",          "city": "Louisville",    "state": "OH", "role": "City Council Member"},
    {"name": "Marcus Mcintyre",      "city": "Indian Trail",  "state": "NC", "role": "Town Council Member"},
    {"name": "Candace Hunziker",     "city": "Pittsboro",     "state": "NC", "role": "Town Council Member"},
    {"name": "Kevin Edmonds",        "city": "Dickinson",     "state": "TX", "role": "City Council Member"},
    {"name": "Claudia Zapata",       "city": "Kyle",          "state": "TX", "role": "City Council Member"},
    {"name": "Arjenae Jones",        "city": "Greenville",    "state": "NC", "role": "City Council Member"},
    {"name": "Jess Hall",            "city": "Lago Vista",    "state": "TX", "role": "City Council Member"},
    {"name": "Matt Kadas",           "city": "Hartville",     "state": "OH", "role": "Village Council Member"},
    {"name": "Kristen Angelo",       "city": "Walbridge",     "state": "OH", "role": "Village Council Member"},
    {"name": "Mark Huddleston",      "city": "Mount Vernon",  "state": "TX", "role": "City Council Member"},
    {"name": "Michael Martinez",     "city": "Sandy Oaks",    "state": "TX", "role": "City Council Member"},

    # ── Tier 3: Marginal / Needs discovery ───────────────────────────────────
    {"name": "Fred Ilarraza",        "city": "Marvin",        "state": "NC", "role": "Village Council Member"},
    {"name": "Michael Benson",       "city": "Lexington",     "state": "OH", "role": "City Council Member"},
    {"name": "Mark Reams",           "city": "Marysville",    "state": "OH", "role": "City Council Member"},
    {"name": "Todd Gordon",          "city": "Lima",          "state": "OH", "role": "City Council Member"},
    {"name": "Patrick Shea",         "city": "North Olmsted", "state": "OH", "role": "City Council Member"},
    {"name": "Christopher Gibbs",    "city": "Palestine",     "state": "TX", "role": "City Council Member"},
    {"name": "Byron Bellman",        "city": "Gibsonville",   "state": "NC", "role": "Town Council Member"},
    {"name": "Mark Cozy",            "city": "Canal Fulton",  "state": "OH", "role": "City Council Member"},
    {"name": "Gregory Drew",         "city": "Vermilion",     "state": "OH", "role": "City Council Member"},
    {"name": "Heather Basil",        "city": "Mount Sterling","state": "OH", "role": "Village Council Member"},
    {"name": "Brian Spitznagel",     "city": "Walton Hills",  "state": "OH", "role": "Village Council Member"},
    {"name": "Cody Mathews",         "city": "Hillsboro",     "state": "OH", "role": "City Council Member"},
    {"name": "Abbie Bosak",          "city": "Poland",        "state": "OH", "role": "Village Council Member"},
    {"name": "Laurie Mack",          "city": "Granite Quarry","state": "NC", "role": "Town Council Member"},
    {"name": "Jon Van De Riet",      "city": "Stallings",     "state": "NC", "role": "Town Council Member"},
    {"name": "Ixtlazihuatl Vasquez", "city": "Refugio",       "state": "TX", "role": "City Council Member"},
    {"name": "Edwina Agee",          "city": "Maple Heights", "state": "OH", "role": "City Council Member"},
    {"name": "Berry Phillips",       "city": "Coleman",       "state": "TX", "role": "City Council Member"},
    {"name": "Chad Deese",           "city": "Pembroke",      "state": "NC", "role": "Town Council Member"},
]


def city_slug(city: str, state: str) -> str:
    """Return the filesystem slug for a city, e.g. 'Indian Trail', 'NC' → 'indian-trail-NC'."""
    return f"{city.lower().replace(' ', '-')}-{state}"


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
