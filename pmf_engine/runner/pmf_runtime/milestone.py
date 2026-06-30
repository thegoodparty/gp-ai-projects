from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Sibling of session.jsonl after upload. The runner's _collect_log_files
# special-cases this basename so it lands at <exp>/<runId>/logs/milestones.jsonl
# (a bare sibling of session.jsonl) rather than nested under workspace/logs/.
_LOG_BASENAME = "milestones.jsonl"


def milestone(name: str) -> None:
    """Mark the start of a named phase of the run.

    Appends one JSON line ({"ts": <UTC ISO8601>, "name": <name>}) to
    <workspace>/logs/milestones.jsonl. The runner uploads that file to S3
    alongside session.jsonl, and cost analysis joins each turn to the most
    recent milestone marker at/before the turn's timestamp.

    There is NO broker call and NO network egress — this is a local append the
    runner already ships. A milestone() call must never break a run, so every
    failure (bad name, unwritable workspace, full disk) is swallowed and logged.
    """
    try:
        workspace = os.environ.get("WORKSPACE_DIR") or os.environ.get("PMF_WORKSPACE", "/workspace")
        logs_dir = os.path.join(workspace, "logs")
        os.makedirs(logs_dir, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "name": str(name),
        }
        with open(os.path.join(logs_dir, _LOG_BASENAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:
        logger.warning("milestone(%r) failed: %s: %s", name, type(exc).__name__, exc)
