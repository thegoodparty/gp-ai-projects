#!/bin/bash
set -e

echo "Starting PMF Engine Runner..."
echo "Experiment: ${EXPERIMENT_ID:-not set}"
echo "Run ID: ${RUN_ID:-not set}"
echo "Candidate: ${CANDIDATE_ID:-not set}"
echo "Harness: ${HARNESS:-claude_sdk}"
echo "Workspace: ${WORKSPACE_DIR:-/workspace}"

if [ -z "$EXPERIMENT_ID" ]; then
    echo "ERROR: EXPERIMENT_ID environment variable is required"
    exit 1
fi

if [ -z "$INSTRUCTION" ]; then
    echo "INSTRUCTION not set — will load from registry inside container"
fi

python -m pmf_engine.runner.main
