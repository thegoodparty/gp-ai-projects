#!/bin/bash
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$SCRIPT_DIR/.build"
ZIP_PATH="$SCRIPT_DIR/lambda.zip"

echo "Building campaign-plan-lambda..."
echo "Script dir: $SCRIPT_DIR"
echo "Build dir: $BUILD_DIR"

# Clean previous build
rm -rf "$BUILD_DIR" "$ZIP_PATH"
mkdir -p "$BUILD_DIR"

# Install Python dependencies into build dir
echo "Installing dependencies..."
uv export --only-group campaign-plan-lambda --no-hashes --no-header --no-emit-project --frozen \
    | uv pip install \
    --target "$BUILD_DIR" \
    --python-platform x86_64-unknown-linux-gnu \
    --python-version 3.12 \
    -r - \
    --quiet

# Copy campaign_plan_lambda package
echo "Copying campaign_plan_lambda package..."
mkdir -p "$BUILD_DIR/campaign_plan_lambda"
cp "$SCRIPT_DIR"/__init__.py "$BUILD_DIR/campaign_plan_lambda/"
cp "$SCRIPT_DIR"/handler.py "$BUILD_DIR/campaign_plan_lambda/"
cp "$SCRIPT_DIR"/output.py "$BUILD_DIR/campaign_plan_lambda/"
cp "$SCRIPT_DIR"/event_generator.py "$BUILD_DIR/campaign_plan_lambda/"

# Copy shared modules from project root
echo "Copying shared modules..."
if [ -d "$PROJECT_ROOT/shared" ]; then
    cp -r "$PROJECT_ROOT/shared" "$BUILD_DIR/shared"
else
    echo "ERROR: shared/ folder not found at $PROJECT_ROOT/shared"
    exit 1
fi

# Create top-level handler.py that re-exports for Lambda
cat > "$BUILD_DIR/handler.py" << 'EOF'
from campaign_plan_lambda.handler import handler
EOF

# Clean up unnecessary files
find "$BUILD_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "*.pyc" -delete 2>/dev/null || true
find "$BUILD_DIR" -name "*.dist-info" -type d -exec rm -rf {} + 2>/dev/null || true
find "$BUILD_DIR" -name "tests" -type d -exec rm -rf {} + 2>/dev/null || true

# Create zip
echo "Creating zip..."
cd "$BUILD_DIR"
zip -r "$ZIP_PATH" . -x "*.pyc" "__pycache__/*" -q

# Report size
ZIP_SIZE=$(du -sh "$ZIP_PATH" | cut -f1)
echo ""
echo "Lambda zip created: $ZIP_PATH"
echo "Zip size: $ZIP_SIZE"
echo ""

# Check approximate unzipped size
UNZIPPED_BYTES=$(unzip -l "$ZIP_PATH" | tail -1 | awk '{print $1}')
UNZIPPED_MB=$((UNZIPPED_BYTES / 1048576))
echo "Approximate unzipped size: ${UNZIPPED_MB}MB"

if [ "$UNZIPPED_MB" -gt 250 ]; then
    echo "WARNING: Exceeds 250MB Lambda unzipped limit!"
    exit 1
else
    echo "OK: Within 250MB Lambda unzipped limit."
fi
