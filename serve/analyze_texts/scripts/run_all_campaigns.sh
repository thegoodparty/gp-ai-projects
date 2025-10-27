#!/bin/bash

campaigns=(
  "berkley"
  "cara-burnsville"
  "heather-ghaps"
  "japjeet-livingston"
  "joanna-missouri-city"
  "jonathan-north-las-vegas"
  "josh-minooka"
)

echo "=========================================="
echo "Running analyze_texts pipeline for all campaigns"
echo "=========================================="
echo ""

for campaign in "${campaigns[@]}"; do
  echo "=========================================="
  echo "Processing: $campaign"
  echo "=========================================="

  uv run serve/analyze_texts/scripts/run_pipeline.py --campaign "$campaign"

  status=$?

  if [ $status -eq 0 ]; then
    echo "✅ $campaign completed successfully"
  else
    echo "❌ $campaign failed with status $status"
  fi

  echo ""
done

echo "=========================================="
echo "All campaigns processed!"
echo "=========================================="
