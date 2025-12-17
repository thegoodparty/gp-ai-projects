#!/usr/bin/env python3

import json
import os
from datetime import datetime
from pathlib import Path

from shared.logger import get_logger

logger = get_logger(__name__)

DEFAULT_OUTPUT_DIR = "/app/serve/v1_pipeline/output"


def get_output_dir() -> str:
    return os.environ.get(
        "PIPELINE_OUTPUT_DIR",
        str(Path(__file__).parent.parent / "output")
    )


def save_events(events: list[dict], output_dir: str | None = None) -> str:
    output_dir = output_dir or get_output_dir()
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')

    events_dir = Path(output_dir) / "events"
    events_dir.mkdir(parents=True, exist_ok=True)

    events_file = events_dir / f"events_{timestamp}.json"

    with open(events_file, 'w') as f:
        json.dump(events, f, indent=2)

    logger.info(f"✅ Saved {len(events)} event(s) to {events_file}")
    return str(events_file)
