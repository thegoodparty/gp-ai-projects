#!/bin/bash
set -e

CAMPAIGN="${1}"

show_usage() {
    cat <<EOF
🚀 V1 Pipeline Local Development Helper

Usage: $0 <campaign>

Arguments:
  campaign    Campaign name (berkley, cara, josh, etc.)

Examples:
  $0 berkley
  $0 cara
  $0 josh

Environment Variables (optional):
  S3_OUTPUT_BUCKET       S3 bucket for event files (default: serve-analyze-data-dev)

EOF
    exit 1
}

if [ -z "$CAMPAIGN" ]; then
    echo "❌ Error: Campaign name required"
    echo ""
    show_usage
fi

echo "🚀 V1 Pipeline Local Development"
echo "=================================="
echo "Campaign: $CAMPAIGN"
echo ""

# Check if we're in the right directory
if [ ! -d "serve/v1_pipeline" ]; then
    echo "❌ Error: Must run from gp-ai-projects root directory"
    echo "Current directory: $(pwd)"
    exit 1
fi

# Get absolute path to project root
PROJECT_ROOT="$(pwd)"

# Create temporary input directory
TEMP_INPUT_DIR=$(mktemp -d)
trap "rm -rf $TEMP_INPUT_DIR" EXIT

# Copy only campaign-specific CSV files
echo "📂 Scanning for campaign files..."
CAMPAIGN_LOWER=$(echo "$CAMPAIGN" | tr '[:upper:]' '[:lower:]')

# Check if campaign-specific subdirectory exists
if [ -d "serve/input/$CAMPAIGN_LOWER" ]; then
    echo "✅ Found campaign subdirectory: serve/input/$CAMPAIGN_LOWER"
    cp serve/input/"$CAMPAIGN_LOWER"/*.csv "$TEMP_INPUT_DIR/" 2>/dev/null || true
else
    # Fall back to pattern matching in flat directory (case-insensitive)
    echo "📂 Searching for files matching: *$CAMPAIGN_LOWER* (case-insensitive)"
    find serve/input -maxdepth 1 -iname "*${CAMPAIGN_LOWER}*" -name "*.csv" -exec cp {} "$TEMP_INPUT_DIR" \; 2>/dev/null || true
fi

FILE_COUNT=$(ls -1 "$TEMP_INPUT_DIR"/*.csv 2>/dev/null | wc -l)
if [ "$FILE_COUNT" -eq 0 ]; then
    echo ""
    echo "❌ No CSV files found for campaign: $CAMPAIGN"
    echo ""
    echo "Available files in serve/input/:"
    ls -1 serve/input/*.csv 2>/dev/null | head -10 || echo "  (empty)"
    echo ""
    echo "💡 To fix this issue:"
    echo "  1. Add CSV files for this campaign to: serve/input/"
    echo "  2. Ensure filenames contain '$CAMPAIGN_LOWER'"
    echo "  3. OR create a subdirectory: serve/input/$CAMPAIGN_LOWER/"
    exit 1
fi

echo "✅ Found $FILE_COUNT CSV file(s)"
echo ""
echo "Files to process:"
ls -1 "$TEMP_INPUT_DIR"/*.csv | xargs -n1 basename
echo ""

# Set required environment variables
export S3_OUTPUT_BUCKET="${S3_OUTPUT_BUCKET:-serve-analyze-data-dev}"
export SQS_QUEUE_URL="${SQS_QUEUE_URL:-}"

# Create temporary config with custom input directory
TEMP_CONFIG=$(mktemp -d)/pipeline_config.yaml
cat > "$TEMP_CONFIG" <<EOF
pipeline:
  mode: "integrated"
  checkpoint_enabled: true

consolidation:
  input_dir: "$TEMP_INPUT_DIR"
  output_dir: "$PROJECT_ROOT/serve/v1_pipeline/output/consolidated"

clustering:
  enabled: true
  min_messages_for_clustering: 1
  skip_on_error: true

sqs_events:
  enabled: true
  publish_to_sqs: false
  s3_bucket: "$S3_OUTPUT_BUCKET"
  s3_prefix: "events"
  publish_top_n: 3
  min_unique_respondents: 1

logging:
  level: "INFO"
  log_to_file: true
  log_file: "$PROJECT_ROOT/serve/v1_pipeline/logs/pipeline.log"
EOF

echo "Configuration:"
echo "  Input Directory: $TEMP_INPUT_DIR"
echo "  Output Directory: $PROJECT_ROOT/serve/v1_pipeline/output/"
echo "  S3 Output Bucket: $S3_OUTPUT_BUCKET"
echo ""

# Create output directories
mkdir -p "$PROJECT_ROOT/serve/v1_pipeline/output/consolidated"
mkdir -p "$PROJECT_ROOT/serve/v1_pipeline/output/discovery_reports"
mkdir -p "$PROJECT_ROOT/serve/v1_pipeline/output/dynamodb_preview"
mkdir -p "$PROJECT_ROOT/serve/v1_pipeline/output/events"
mkdir -p "$PROJECT_ROOT/serve/v1_pipeline/logs"

echo "🚀 Running pipeline..."
echo "=================================="
echo ""

ENVIRONMENT=development uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign "$CAMPAIGN" \
  --config "$TEMP_CONFIG"

PIPELINE_EXIT_CODE=$?

echo ""
echo "=================================="
if [ $PIPELINE_EXIT_CODE -eq 0 ]; then
    echo "✅ Pipeline completed successfully!"
    echo ""
    echo "📁 Output files:"
    echo "  Consolidated: $PROJECT_ROOT/serve/v1_pipeline/output/consolidated/"
    echo "  Discovery Reports: $PROJECT_ROOT/serve/v1_pipeline/output/discovery_reports/"
    echo "  DynamoDB Preview: $PROJECT_ROOT/serve/v1_pipeline/output/dynamodb_preview/"
    echo "  Events: $PROJECT_ROOT/serve/v1_pipeline/output/events/"
    echo ""
    echo "View files:"
    echo "  ls -lh $PROJECT_ROOT/serve/v1_pipeline/output/consolidated/"
    echo "  ls -lh $PROJECT_ROOT/serve/v1_pipeline/output/discovery_reports/"
else
    echo "❌ Pipeline failed with exit code: $PIPELINE_EXIT_CODE"
    echo ""
    echo "Check logs for details:"
    echo "  tail -50 $PROJECT_ROOT/serve/v1_pipeline/logs/pipeline.log"
fi

exit $PIPELINE_EXIT_CODE
