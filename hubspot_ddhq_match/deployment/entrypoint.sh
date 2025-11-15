#!/bin/bash
set -e

echo "========================================"
echo "DDHQ Matcher Pipeline"
echo "========================================"

if [[ -z "$S3_OUTPUT_BUCKET" ]]; then
  echo "❌ Error: S3_OUTPUT_BUCKET environment variable is required"
  exit 1
fi

if [[ ! "$S3_OUTPUT_BUCKET" =~ ^[a-z0-9][a-z0-9.-]*[a-z0-9]$ ]]; then
  echo "❌ Error: Invalid S3_OUTPUT_BUCKET format: $S3_OUTPUT_BUCKET"
  echo "   Must contain only lowercase letters, numbers, dots, and hyphens"
  exit 1
fi

if [[ -n "$S3_OUTPUT_PREFIX" ]] && [[ ! "$S3_OUTPUT_PREFIX" =~ ^[a-zA-Z0-9/_-]+$ ]]; then
  echo "❌ Error: Invalid S3_OUTPUT_PREFIX format: $S3_OUTPUT_PREFIX"
  echo "   Must contain only alphanumeric characters, slashes, underscores, and hyphens"
  exit 1
fi

echo "Run ID: ${RUN_ID}"
echo "Environment: ${ENVIRONMENT:-production}"
echo ""
echo "Tables:"
echo "  HubSpot: ${HUBSPOT_TABLE:-dbt.m_general__candidacy}"
echo "  DDHQ: ${DDHQ_TABLE:-dbt.stg_airbyte_source__ddhq_gdrive_election_results}"
echo ""
echo "Embedding Step:"
echo "  Batch Size: ${EMBEDDING_BATCH_SIZE:-100}"
echo "  Max Workers: ${EMBEDDING_MAX_WORKERS:-80}"
echo ""
echo "Matching Step:"
echo "  Batch Size: ${MATCHING_BATCH_SIZE:-1000}"
echo "  Max Workers: ${MATCHING_MAX_WORKERS:-2000}"
echo ""
echo "S3 Output: s3://${S3_OUTPUT_BUCKET}/${S3_OUTPUT_PREFIX}"
echo "========================================"

# Run pipeline from the project root
cd /app

python hubspot_ddhq_match/run_pipeline.py

EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
  echo ""
  echo "========================================"
  echo "✅ Pipeline completed successfully"
  echo "========================================"
  echo "Uploading results to S3..."

  if python hubspot_ddhq_match/deployment/upload_to_s3.py \
    hubspot_ddhq_match/output/matches.parquet \
    "${S3_OUTPUT_BUCKET}" \
    "${S3_OUTPUT_PREFIX}/matches.parquet"; then
    echo "========================================"
  else
    echo ""
    echo "========================================"
    exit 1
  fi
else
  echo ""
  echo "========================================"
  echo "❌ Pipeline failed with exit code $EXIT_CODE"
  echo "========================================"
fi

exit $EXIT_CODE
