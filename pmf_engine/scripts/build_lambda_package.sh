#!/bin/bash
set -euo pipefail

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

if [ -z "$OUTPUT_DIR" ]; then
  echo "ERROR: OUTPUT_DIR resolved to empty string; refusing to rm -rf ''" >&2
  exit 1
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR"

cp "$PMF_DIR/control_plane/dispatch_handler.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/broker_client.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/scope_derivation.py" "$OUTPUT_DIR/"
cp "$PMF_DIR/control_plane/manifest_loader.py" "$OUTPUT_DIR/"

mkdir -p "$OUTPUT_DIR/shared"
touch "$OUTPUT_DIR/shared/__init__.py"
cp "$SHARED_DIR/logger.py" "$OUTPUT_DIR/shared/"

# Vendor third-party deps that dispatch_handler.py (and its imports) need.
# Lambda runtime provides boto3 only. broker_client needs httpx; dispatch
# uses jsonschema for Draft-07 validation of message["params"] against
# manifest.input_schema. jsonschema's transitive dep `referencing` uses
# `TypeVar(default=...)` — a Python 3.13+ feature — so the Lambda runtime
# MUST be python3.13. rpds-py is a Rust extension; the platform tag MUST
# match Lambda's deployment arch (x86_64). Wrong combo of arch/version =
# silent ImportError at runtime.
python3 -m pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.13 \
  --only-binary=:all: \
  --target "$OUTPUT_DIR" \
  --upgrade \
  httpx \
  jsonschema \
  >/dev/null

echo "Lambda package built: $OUTPUT_DIR"
