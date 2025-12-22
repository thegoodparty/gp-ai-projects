# Silver to Gold Migration Pipeline (ENG-5041)

Automated pipeline for migrating silver tier candidates to gold tier by updating race-target-details via the GoodParty API.

## Quick Start

### Test Mode (First 5 candidates)
```bash
uv run silver-to-gold-migration/serve-silver-migration/run_complete_pipeline.py \
  path/to/hubspot-export.csv \
  --test
```

### Production Run
```bash
uv run silver-to-gold-migration/serve-silver-migration/run_complete_pipeline.py \
  path/to/hubspot-export.csv \
  --delay 0.5
```

## Prerequisites

### Environment Variables
Create `.env` in `gp-ai-projects/`:
```bash
DATABRICKS_API_KEY=your_databricks_token
DATABRICKS_SERVER_HOSTNAME=your-workspace.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/your-warehouse-id
GOODPARTY_API_TOKEN=your_jwt_token
```

### Input Requirements
HubSpot CSV export with:
- `Record ID` column (HubSpot company ID)
- `Company name` column (candidate name)
- `Election Date` column

## What It Does

1. **Slug Mapping**: Maps HubSpot CSV candidates to GoodParty campaign slugs via Databricks
2. **Date Filtering**: Excludes candidates with future election dates (safety check)
3. **API Processing**: Calls `PUT /api/v1/campaigns/admin/{slug}/race-target-details` for each candidate
4. **Progress Tracking**: Saves progress every 10 requests

## Command Options

```bash
--test                    # Process first 5 candidates only
--delay SECONDS          # Delay between requests (default: 0.5s)
--limit N                # Process only N candidates
--start-index N          # Resume from specific index
--output-dir PATH        # Custom output directory
```

## Output Files

Saved to `data/` directory:
- `candidates_with_slugs_TIMESTAMP.csv` - Mapped candidates (past elections only)
- `results_final_TIMESTAMP.csv` - Final API results with success/failure status
- `results_progress_TIMESTAMP_N.parquet` - Progress checkpoints

## Performance

| Delay | Throughput | Time (1,800 candidates) |
|-------|-----------|------------------------|
| 0.5s  | 120/min   | ~15 minutes (default)  |
| 1.0s  | 60/min    | ~30 minutes            |

## Common Issues

**"GOODPARTY_API_TOKEN not found"**
- Add token to `.env` file (valid JWT from GoodParty admin session)

**"Failed to connect to Databricks"**
- Check `DATABRICKS_API_KEY` and network connectivity

**Low success rate**
- Check `api_error` column in results CSV
- Verify token is valid and candidates have campaigns
