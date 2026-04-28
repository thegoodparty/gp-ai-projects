#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PMF_DIR="$SCRIPT_DIR/.."
REPO_ROOT="$PMF_DIR/.."
SHARED_DIR="$REPO_ROOT/shared"

# Resolve OUTPUT_DIR to an absolute path so `cd "$REPO_ROOT"` below doesn't
# redirect the registry file to the wrong directory.
OUTPUT_DIR_IN="${1:-$PMF_DIR/.lambda_build}"
case "$OUTPUT_DIR_IN" in
  /*) OUTPUT_DIR="$OUTPUT_DIR_IN" ;;
  *)  OUTPUT_DIR="$(pwd)/$OUTPUT_DIR_IN" ;;
esac

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

cp "$PMF_DIR/control_plane/dispatch_handler.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/broker_client.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/scope_derivation.py" "$OUTPUT_DIR/"

# Generate a flat, import-free dispatch_registry.py from the source-tree
# EXPERIMENT_REGISTRY. Single source of truth — drift is impossible.
(cd "$REPO_ROOT" && python3 -m pmf_engine.scripts.generate_flat_dispatch_registry \
    "$OUTPUT_DIR/dispatch_registry.py")

mkdir -p "$OUTPUT_DIR/shared"
touch "$OUTPUT_DIR/shared/__init__.py"
cp "$SHARED_DIR/logger.py" "$OUTPUT_DIR/shared/"

# Vendor third-party deps that dispatch_handler.py (and its imports) need.
# Lambda runtime only provides boto3 by default. broker_client uses httpx.
# Install for the Lambda runtime architecture (arm64, python3.12).
python3 -m pip install \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.12 \
  --only-binary=:all: \
  --target "$OUTPUT_DIR" \
  --upgrade \
  httpx \
  >/dev/null

echo "Lambda package built: $OUTPUT_DIR"
