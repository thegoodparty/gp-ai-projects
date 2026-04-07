"""Tests for pilot_registry.py"""
import pytest
from meeting_pipeline.pilot_registry import PILOT_OFFICIALS, pilot_cities, city_slug


def test_all_officials_have_required_fields():
    for o in PILOT_OFFICIALS:
        assert o.get("name"), f"Missing name: {o}"
        assert o.get("city"), f"Missing city: {o}"
        assert o.get("state"), f"Missing state: {o}"
        assert o.get("role"), f"Missing role: {o}"


def test_state_codes_are_valid():
    valid_states = {"OH", "NC", "TX"}
    for o in PILOT_OFFICIALS:
        assert o["state"] in valid_states, f"Unexpected state '{o['state']}' for {o['name']}"


def test_pilot_cities_are_deduplicated():
    cities = pilot_cities()
    keys = [(c["city"], c["state"]) for c in cities]
    assert len(keys) == len(set(keys)), "pilot_cities() returned duplicates"


def test_city_slug_format():
    assert city_slug("Johnstown", "OH") == "johnstown-OH"
    assert city_slug("Indian Trail", "NC") == "indian-trail-NC"
    assert city_slug("Mount Sterling", "OH") == "mount-sterling-OH"


def test_no_duplicate_officials():
    names = [o["name"] for o in PILOT_OFFICIALS]
    duplicates = [n for n in names if names.count(n) > 1]
    assert not duplicates, f"Duplicate officials: {duplicates}"
