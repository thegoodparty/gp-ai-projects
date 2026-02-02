#!/bin/bash
set -e

echo "Starting Engineer Agent..."
echo "Task ID: ${CLICKUP_TASK_ID:-not set}"
echo "Workspace: ${WORKSPACE_DIR:-/workspace}"

if [ -z "$CLICKUP_TASK_ID" ]; then
    echo "ERROR: CLICKUP_TASK_ID environment variable is required"
    exit 1
fi

python -m engineer_agent.agent.main
