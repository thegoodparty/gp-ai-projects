#!/bin/bash

echo "=== Pipeline Progress Monitor ==="
echo ""

# Check if process is running
if ps aux | grep -v grep | grep "rerun_all_campaigns.py" > /dev/null; then
    echo "✅ Pipeline is RUNNING"
    ps aux | grep -v grep | grep "rerun_all_campaigns.py" | awk '{print "   PID: "$2", CPU: "$3"%, MEM: "$4"%"}'
    echo ""
else
    echo "⚠️  Pipeline is NOT RUNNING (may have completed or failed)"
    echo ""
fi

# Show last 20 lines of log
echo "=== Latest Log Output ==="
if [ -f serve/analyze_texts/rerun_all.log ]; then
    tail -20 serve/analyze_texts/rerun_all.log
else
    echo "No log file found"
fi

echo ""
echo "=== File Timestamps (most recent first) ==="
find serve/analyze_texts/output -name "*_enriched.csv" -type f -exec stat -f "%Sm %N" -t "%Y-%m-%d %H:%M:%S" {} \; 2>/dev/null | sort -r | head -10

echo ""
echo "=== Record ID Check for Recently Updated Files ==="
for file in $(find serve/analyze_texts/output -name "*_enriched.csv" -type f -mmin -30 2>/dev/null); do
    campaign=$(basename $(dirname "$file"))
    total=$(wc -l < "$file" | tr -d ' ')
    unique=$(tail -n +2 "$file" | cut -d',' -f4 | sort -u | wc -l | tr -d ' ')
    echo "  $campaign: $total rows, $unique unique record_ids"
done
