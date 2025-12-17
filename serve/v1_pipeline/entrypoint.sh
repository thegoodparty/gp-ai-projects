#!/bin/bash
set -e

echo "=========================================="
echo "Serve Analyze Pipeline - Docker Container"
echo "=========================================="

log() {
  echo "[$(date +'%Y-%m-%d %H:%M:%S')] $1"
}

PIPELINE_MODE="${PIPELINE_MODE:-cluster}"
log "Pipeline Mode: $PIPELINE_MODE"

if [ -n "$S3_INPUT_PATH" ]; then
  log "Downloading data from S3: $S3_INPUT_PATH"

  mkdir -p /app/serve/input

  if [[ "$S3_INPUT_PATH" == *.csv ]]; then
    aws s3 cp "$S3_INPUT_PATH" /app/serve/input/ --quiet
  else
    aws s3 sync "$S3_INPUT_PATH" /app/serve/input/ --quiet
  fi

  log "Download complete. Files in /app/serve/input/:"
  find /app/serve/input/ -type f -exec ls -lh {} \;

  if [ -z "$CAMPAIGN_NAME" ]; then
    FIRST_CSV=$(find /app/serve/input/ -name "*.csv" -type f | head -n 1)
    if [ -n "$FIRST_CSV" ]; then
      CAMPAIGN_NAME=$(basename "$FIRST_CSV" .csv)
      log "Campaign name extracted from filename: $CAMPAIGN_NAME"
    else
      log "ERROR: No CSV files found and CAMPAIGN_NAME not provided"
      exit 1
    fi
  fi
else
  if [ -z "$CAMPAIGN_NAME" ]; then
    log "ERROR: Either S3_INPUT_PATH or CAMPAIGN_NAME must be provided"
    exit 1
  fi
fi

EXTRA_ARGS=""
if [ "$SKIP_CLUSTERING" = "true" ]; then
  EXTRA_ARGS="$EXTRA_ARGS --skip-clustering"
fi

if [ "$SKIP_CLASSIFICATION" = "true" ]; then
  EXTRA_ARGS="$EXTRA_ARGS --skip-classification"
fi

if [ "$DEBUG" = "true" ]; then
  export ENVIRONMENT="development"
  log "Debug mode enabled"
fi

log "Configuration:"
log "  Campaign: $CAMPAIGN_NAME"
log "  Pipeline Mode: $PIPELINE_MODE"
log "  Environment: $ENVIRONMENT"
log "  Input Dir: /app/serve/input/"
log "  Output Dir: /app/serve/v1_pipeline/output/"

if [ "$PIPELINE_MODE" = "classify" ]; then
  log "  Poll ID: $POLL_ID"
  log "  Question: $QUESTION_TEXT"
  log "  Options: $OPTIONS_JSON"
  log "  Callback Success: $CALLBACK_SUCCESS_URL"
  log "  Callback Failure: $CALLBACK_FAILURE_URL"
fi

log "Starting pipeline execution..."
log "=========================================="

cd /app

if [ "$PIPELINE_MODE" = "classify" ]; then
  python serve/v1_pipeline/scripts/run_pipeline.py \
    --campaign="$CAMPAIGN_NAME" \
    --mode=classify \
    --poll-id="$POLL_ID" \
    --question-text="$QUESTION_TEXT" \
    --options-json="$OPTIONS_JSON" \
    --callback-success-url="${CALLBACK_SUCCESS_URL:-}" \
    --callback-failure-url="${CALLBACK_FAILURE_URL:-}" \
    $EXTRA_ARGS
else
  python serve/v1_pipeline/scripts/run_pipeline.py \
    --campaign="$CAMPAIGN_NAME" \
    --mode=cluster \
    $EXTRA_ARGS
fi

PIPELINE_EXIT_CODE=$?

log "=========================================="

if [ $PIPELINE_EXIT_CODE -eq 0 ]; then
  log "Pipeline completed successfully!"

  OUTPUT_DIR="/app/serve/v1_pipeline/output"
  if [ -d "$OUTPUT_DIR" ]; then
    log "Generated files:"
    find "$OUTPUT_DIR" -type f -exec ls -lh {} \; | tail -n +2
  fi

  if [ -n "$S3_OUTPUT_PATH" ]; then
    log "Uploading results to S3: $S3_OUTPUT_PATH"
    aws s3 sync "$OUTPUT_DIR" "$S3_OUTPUT_PATH" --quiet
    log "Upload complete!"
  fi

  exit 0
else
  log "ERROR: Pipeline failed with exit code $PIPELINE_EXIT_CODE"
  exit $PIPELINE_EXIT_CODE
fi
