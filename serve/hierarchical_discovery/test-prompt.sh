#!/bin/bash

# Quick wrapper script for testing prompt versions
# Usage: ./test-prompt.sh [version-name]

cd "$(dirname "$0")/../.."

VERSION=${1:-"unlabeled"}

echo "Testing prompt version: $VERSION"
echo ""

uv run serve/hierarchical_discovery/test_prompt_version.py \
    --version "$VERSION" \
    --save-summary \
    "$@"


