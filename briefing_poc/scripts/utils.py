"""Shared utilities for briefing pipeline scripts."""

import json
from pathlib import Path


def load_json(path: Path) -> dict | None:
    """Load a JSON file, return None if it doesn't exist."""
    if not path.exists():
        print(f"  WARNING: {path.name} not found")
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
