# Prompt Testing Guide

Quick guide for testing new prompt versions in the hierarchical discovery pipeline.

## Quick Start

The easiest way to test a new prompt version:

```bash
cd serve/hierarchical_discovery
./test-prompt.sh "v2-action-oriented"
```

This will:
- Run the pipeline on 4 standard test files (small, medium, large)
- Generate a comparison report with costs and themes
- Save a JSON summary with timestamp
- Show sample analyses from your new prompt

## Test Files

The script uses these standard test files by default:

| File | Size | Messages | Description |
|------|------|----------|-------------|
| `019a367f-b7c2-71a3-b140-adefc9b7ba0a` | 1.2K | ~0-2 | Very small (often empty) |
| `019a5083-c166-7170-917a-78bde83ee0d9` | 1.3K | ~2 | Minimal messages |
| `019ab7bc-7912-7f22-bb59-5f35d0640595` | 14K | ~28 | Good test size |
| `019a604c-898a-7481-a868-c09fa080759b` | 75K | ~230 | Full scale test |

## Usage Examples

### Basic test with version label
```bash
./test-prompt.sh "v3-constituent-focused"
```

### Test only medium and large files
```bash
uv run serve/hierarchical_discovery/test_prompt_version.py \
    --version "v3-test" \
    --files 019ab7bc-7912-7f22-bb59-5f35d0640595 019a604c-898a-7481-a868-c09fa080759b
```

### Full analysis (not quick test)
```bash
uv run serve/hierarchical_discovery/test_prompt_version.py \
    --version "v3-full" \
    --no-quick-test \
    --save-summary
```

### Custom timeout for large files
```bash
uv run serve/hierarchical_discovery/test_prompt_version.py \
    --timeout 600 \
    --save-summary
```

## Understanding the Output

The script will show:

### 1. **Per-File Results**
```
Results for 019ab7bc-7912-7f22-bb59-5f35d0640595
------------------------------------------------
✓ Pipeline completed in 15.2s

Report Summary:
  Messages: 28
  Clusters: 11
  Themes: 10
  Avg per cluster: 2.1
  Cost: $0.0032

Top Themes:
  School Funding & Taxes (3 people), Education and Safety (3 people)

Sample Theme Analysis (from new prompt):
  1. Education Funding Support
     Summary: Your constituents want increased support and funding...
     Analysis: Your constituents express a clear need for stronger...
```

### 2. **Overall Summary**
```
Overall Summary
===============
Total Duration: 67.3s
Successful Runs: 3/4
Total Cost: $0.0219
```

### 3. **Output Locations**
- Reports: `output/reports/`
- Exports: `output/exports/`
- Test summaries: `output/test_summary_*.json`

## Comparing Prompt Versions

### Method 1: Compare summaries directly
```bash
# Save each test with version labels
./test-prompt.sh "v1-baseline"
# Update prompt in Braintrust
./test-prompt.sh "v2-action-oriented"
# Update prompt again
./test-prompt.sh "v3-constituent-focused"

# Compare JSON summaries
ls -lt output/test_summary_*.json | head -3
```

### Method 2: Compare specific metrics
Look at the saved JSON files to compare:
- `total_cost` - Cost efficiency
- `report_data.themes_found` - Theme diversity
- Sample analyses - Prompt quality

### Method 3: Manual review
Check the actual exports and reports:
```bash
# View latest reports
ls -lt output/reports/ | head -10

# Check specific export for prompt quality
head -50 output/exports/multi_cluster_results_019ab7bc_*.csv
```

## What to Look For

When evaluating prompt changes, assess:

### ✅ **Quality Indicators**
- **Clarity**: Are themes clear and actionable?
- **Directness**: Is the language appropriate for the audience (elected officials)?
- **Specificity**: Are issues well-defined?
- **Actionability**: Can the official act on this information?

### ✅ **Consistency Indicators**
- **Cluster count**: Similar ranges across runs
- **Message distribution**: Reasonable avg per cluster
- **Cost**: Not dramatically higher

### ❌ **Red Flags**
- Themes that are too vague ("Issues" or "Concerns")
- Analysis that just repeats the theme
- Significantly higher costs without quality improvement
- Themes that don't match the actual message content

## Workflow

1. **Baseline test**: Run test with current prompt
2. **Update prompt**: Change prompt in Braintrust
3. **Test new version**: Run test with version label
4. **Compare**: Review outputs side-by-side
5. **Iterate**: Refine and test again

## Tips

- Use descriptive version labels: `v2-action-oriented` not `test2`
- Test after every significant prompt change
- Keep a log of what you changed in each version
- Review at least the medium and large file results
- Check both the summary stats AND sample analyses

## Advanced Usage

### Custom test files
```bash
uv run serve/hierarchical_discovery/test_prompt_version.py \
    --files my-custom-file-uuid another-file-uuid \
    --version "custom-test"
```

### Integration with Braintrust
The pipeline automatically loads prompts from Braintrust if configured.
Make sure your `BRAINTRUST_API_KEY` is set in `.env`.

### Batch testing multiple versions
```bash
#!/bin/bash
# test-all-versions.sh

versions=("v1-baseline" "v2-direct" "v3-actionable")

for version in "${versions[@]}"; do
    echo "Testing $version..."
    # Update Braintrust prompt here (or manually between runs)
    ./test-prompt.sh "$version"
    echo "Waiting 5s before next test..."
    sleep 5
done

echo "All tests complete. Compare results:"
ls -lt output/test_summary_*.json | head -${#versions[@]}
```

## Troubleshooting

### Pipeline fails
- Check `--timeout` setting (large files may need 300-600s)
- Verify Braintrust API key is set
- Check available system resources

### Inconsistent results
- Re-run the same version multiple times
- Check if message counts vary (data loading issue)
- Verify prompt is loading correctly from Braintrust

### Missing output files
- Check that output directory exists
- Verify permissions on output directory
- Look for errors in pipeline stdout/stderr

## Quick Reference

```bash
# Fastest test (just medium file)
uv run serve/hierarchical_discovery/test_prompt_version.py \
    --files 019ab7bc-7912-7f22-bb59-5f35d0640595

# Full test with all bells and whistles
./test-prompt.sh "v3-final" --save-summary

# Compare two recent test outputs
diff output/test_summary_20251211_145000.json output/test_summary_20251211_150000.json

# View latest export
head -30 $(ls -t output/exports/*.csv | head -1)
```


