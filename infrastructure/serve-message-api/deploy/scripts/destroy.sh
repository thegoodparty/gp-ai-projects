#!/bin/bash

# Simple wrapper script for destroying infrastructure
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Call deploy.sh with destroy flag
"$SCRIPT_DIR/deploy.sh" --destroy "$@"