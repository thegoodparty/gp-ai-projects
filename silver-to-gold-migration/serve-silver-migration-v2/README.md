# Silver to Gold Migration Pipeline V2 (ENG-5041)

Simplified pipeline for migrating pre-vetted silver tier candidates to gold tier by updating race-target-details via the GoodParty API.

## Data Source

The input file `offline_data/gpdb_public_campaign.csv` (19,250 slugs) was generated from the GoodParty production database using the following query:

```sql
SELECT c.*
FROM campaign AS c
JOIN path_to_victory AS p
  ON p.campaign_id = c.id
WHERE to_date(
        NULLIF(
          substring(c.details->>'electionDate' from '^[[:space:]]*([0-9]{4}-[0-9]{2}-[0-9]{2})'),
          ''
        ),
        'YYYY-MM-DD'
      ) < CURRENT_DATE
  AND p.data->>'electionLocation' LIKE '%##%'
  AND c.details ? 'positionId'
  AND btrim(coalesce(c.details->>'positionId', '')) <> '';
```

**Query Filters:**
- Elections with dates in the past (< CURRENT_DATE)
- Campaigns with path_to_victory data containing location markers (`##`)
- Campaigns with valid positionId in details

This pre-filtered dataset means:
- No Databricks queries needed
- No election date filtering required
- Direct API processing only

## Quick Start

### Test Mode (First 5 slugs)
```bash
uv run silver-to-gold-migration/serve-silver-migration-v2/run_pipeline_v2.py \
  offline_data/gpdb_public_campaign.csv \
  --test
```

### Production Run
```bash
uv run silver-to-gold-migration/serve-silver-migration-v2/run_pipeline_v2.py \
  offline_data/gpdb_public_campaign.csv \
  --delay 0.5
```

## Prerequisites

### Environment Variables
Create `.env` in `gp-ai-projects/`:
```bash
GOODPARTY_API_TOKEN=your_jwt_token
```

### Input Requirements
Plain CSV file with one slug per line (no headers).

## What It Does

1. **Load Slugs**: Reads plain text CSV (19,250 slugs)
2. **API Processing**: Calls `PUT /api/v1/campaigns/admin/{slug}/race-target-details` for each
3. **Progress Tracking**: Saves progress every 10 requests

## Command Options

```bash
--test                    # Process first 5 slugs only
--delay SECONDS          # Delay between requests (default: 0.5s)
--limit N                # Process only N slugs
--start-index N          # Resume from specific index
--output-dir PATH        # Custom output directory
```

## Output Files

Saved to `data/` directory:
- `results_final_TIMESTAMP.csv` - Final API results with success/failure status
- `results_progress_TIMESTAMP_N.parquet` - Progress checkpoints

## Performance

| Delay | Throughput | Time (19,250 slugs) |
|-------|-----------|---------------------|
| 0.5s  | 120/min   | ~2.7 hours          |
| 1.0s  | 60/min    | ~5.4 hours          |
| 0.25s | 240/min   | ~1.3 hours          |

## Differences from V1

V1 Pipeline:
- Starts with HubSpot CSV export
- Queries Databricks for slug mapping
- Filters by election dates
- Recovers missing slugs via name matching
- ~15 minutes for 1,800 candidates

V2 Pipeline:
- Starts with pre-vetted slug list from production DB
- No Databricks queries needed
- No date filtering (already filtered in SQL)
- Direct API processing only
- ~2.7 hours for 19,250 slugs

## Common Issues

**"GOODPARTY_API_TOKEN not found"**
- Add token to `.env` file (valid JWT from GoodParty admin session)

**Low success rate**
- Check `api_error` column in results CSV
- Verify token is valid and hasn't expired
