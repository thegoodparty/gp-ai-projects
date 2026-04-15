#!/bin/bash
set -e

OUTPUT_DIR="${1:-.lambda_build}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PMF_DIR="$SCRIPT_DIR/.."
REPO_ROOT="$PMF_DIR/.."
SHARED_DIR="$REPO_ROOT/shared"

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

cp "$PMF_DIR/control_plane/dispatch_handler.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/callback_handler.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/param_screening.py" "$OUTPUT_DIR/"

# Generate a flat, import-free dispatch_registry.py from the source-tree
# EXPERIMENT_REGISTRY. Single source of truth — drift is impossible.
(cd "$REPO_ROOT" && python3 -m pmf_engine.scripts.generate_flat_dispatch_registry \
    "$OUTPUT_DIR/dispatch_registry.py")

mkdir -p "$OUTPUT_DIR/shared"
touch "$OUTPUT_DIR/shared/__init__.py"
cp "$SHARED_DIR/logger.py" "$OUTPUT_DIR/shared/"

echo "Lambda package built: $OUTPUT_DIR"
