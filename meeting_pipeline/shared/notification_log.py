"""
notification_log.py — Structured event logging for the collection agent.

All events emit JSON to stderr (→ CloudWatch in Lambda/Fargate).

Event schema:
    {
        "event_type": "REPLAY_SUCCESS",
        "city": "Loveland",
        "state": "OH",
        "ts": "2026-04-02T12:00:00+00:00",
        ...detail fields...
    }
"""

import json
import sys
from datetime import datetime, timezone
from typing import Any

from meeting_pipeline.shared.storage import StorageBackend


# Valid event types
MIGRATION_DETECTED = "MIGRATION_DETECTED"
COLLECTOR_NEEDED = "COLLECTOR_NEEDED"
NO_PORTAL = "NO_PORTAL"
COLLECTION_FAILED = "COLLECTION_FAILED"
NAV_CONFIG_SAVED = "NAV_CONFIG_SAVED"
REPLAY_SUCCESS = "REPLAY_SUCCESS"
COLLECTION_SUCCESS = "COLLECTION_SUCCESS"
DISCOVERY_STARTED = "DISCOVERY_STARTED"
DISCOVERY_COMPLETE = "DISCOVERY_COMPLETE"
HEALTH_CHECK_STARTED = "HEALTH_CHECK_STARTED"
HEALTH_CHECK_COMPLETE = "HEALTH_CHECK_COMPLETE"


def log_event(
    event_type: str,
    city: str,
    state: str,
    storage: StorageBackend | None = None,
    logs_prefix: str = "meeting_pipeline/logs",
    **detail: Any,
) -> dict:
    """
    Emit a structured event log entry.

    Prints to stderr (captured by CloudWatch in cloud deployments).

    Returns the payload dict (useful for testing).
    """
    payload: dict[str, Any] = {
        "event_type": event_type,
        "city": city,
        "state": state,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    payload.update(detail)

    # stderr → CloudWatch in Lambda/Fargate
    print(json.dumps(payload), file=sys.stderr)

    return payload
