# HubSpot-Google Sheets Race Matching Pipeline

High-performance race-to-race matching system that connects HubSpot candidate offices to Google Sheets race records using semantic similarity and LLM validation.

## Overview

This pipeline matches **candidate office names** from HubSpot companies to **race names** from Google Sheets, filling the empty `race_id` column in Google Sheets data.

### Key Features

- **Date + State + Election Type Partitioned FAISS** - Pre-built indices for efficient exact matching
- **Semantic Similarity** - Gemini embeddings for race/office name matching
- **LLM Validation** - Gemini Flash for intelligent match confirmation with confidence scoring
- **High Concurrency** - ThreadPoolExecutor + asyncio for 1500 concurrent workers
- **Cost Efficient** - ~$0.0007 per 50 records

## Architecture

```
HubSpot Companies → Temporal Filtering → FAISS Partitions → LLM Validation → Match Results
     (37,986)              (16,252)         (75 partitions)     (70% threshold)
```

### Matching Strategy

1. **FAISS Partitioning**: Pre-build indices by `{date}_{state}_{election_type}`
2. **Exact Filtering**: Only search within exact date+state+election_type matches
3. **Semantic Search**: Find top 5 race name candidates using L2 distance
4. **LLM Validation**: Gemini validates office-to-race match with confidence scoring

### Election Type Normalization

Pattern-based matching for consistency:
- `'%primary%'` → `'primary'`
- `'%runoff%'` → `'runoff'`
- `'%general%'` → `'general'`
- Default → `'general'`

## Prerequisites

Before running the pipeline, ensure you have:

1. **Python environment** with `uv` package manager installed
2. **Required API credentials** in `/Users/collinpark/work/gp-ai-projects/.env`:
   ```bash
   GEMINI_API_KEY=your_gemini_api_key
   DDHQ_MATCHER_GOOGLE_CLIENT_ID=your_google_client_id
   DDHQ_MATCHER_GOOGLE_CLIENT_SECRET=your_google_client_secret
   DATABRICKS_API_KEY=your_databricks_key
   DATABRICKS_SERVER_HOSTNAME=your_workspace_hostname
   DATABRICKS_HTTP_PATH=your_sql_warehouse_path
   ```

3. **Google OAuth authentication**:
   - First run will open a browser for Google OAuth
   - Credentials are cached in `token.pickle` for subsequent runs
   - No browser prompt needed after first authentication

## Quick Start

### First Time Setup

Follow these steps to set up and run the pipeline:

**Step 1: Verify Prerequisites**

Ensure `uv` is installed:
```bash
uv --version
```

If not installed, install via: `curl -LsSf https://astral.sh/uv/install.sh | sh`

**Step 2: Navigate to Project Directory**
```bash
cd /Users/collinpark/work/gp-ai-projects/hubspot_ddhq_match/google_sheets_matcher
```

**Step 3: Verify Environment Variables**

The `.env` file should already exist at `/Users/collinpark/work/gp-ai-projects/.env` with the required credentials:

```bash
# Verify .env file exists and has required variables
grep -E "GEMINI_API_KEY|DDHQ_MATCHER_GOOGLE|DATABRICKS" /Users/collinpark/work/gp-ai-projects/.env
```

**Required variables:**
- `GEMINI_API_KEY` - For LLM validation and embeddings
- `DDHQ_MATCHER_GOOGLE_CLIENT_ID` - For Google Sheets OAuth
- `DDHQ_MATCHER_GOOGLE_CLIENT_SECRET` - For Google Sheets OAuth
- `DATABRICKS_API_KEY` - For HubSpot data extraction
- `DATABRICKS_SERVER_HOSTNAME` - Databricks workspace URL
- `DATABRICKS_HTTP_PATH` - SQL warehouse path

**Note:** If you're setting up from scratch and need Google OAuth credentials, ask the project maintainer for the existing OAuth credentials, or create new ones at [Google Cloud Console](https://console.cloud.google.com/apis/credentials).

**Step 4: Install Dependencies**
```bash
# From the google_sheets_matcher directory
uv sync
```

This installs all required Python packages.

**Step 5: Run First Test**

Run the pipeline in test mode (processes 50 records):
```bash
ENVIRONMENT=test uv run run_full_pipeline.py
```

**Expected on first run:**
- Browser will open for Google OAuth authentication
- Authenticate with your Google account
- Credentials saved to `token.pickle` (no browser needed on subsequent runs)
- Pipeline runs all 5 steps
- Results saved to `output/` directory

**Step 6: Verify Success**

Check that output files were created:
```bash
ls -lh output/hubspot_googlesheets_race_matches_latest.parquet
```

If successful, you're ready to run production!

### Run Complete Pipeline (Recommended)

**Single command to run all 5 steps:**

```bash
# Test mode (50 HubSpot records for embeddings and matching)
ENVIRONMENT=test uv run run_full_pipeline.py

# Production mode (all 16,252 records - takes ~20 minutes)
ENVIRONMENT=production uv run run_full_pipeline.py
```

The pipeline script automatically runs all 5 steps in sequence:
1. Data Extraction (from Databricks + Google Sheets)
2. Data Cleaning (normalize election types, expand records)
3. Temporal Filtering (filter to matching dates)
4. Embedding Generation (semantic embeddings)
5. Production Matching (FAISS + LLM validation)

**Output**: `output/hubspot_googlesheets_race_matches_latest.parquet` and timestamped TSV

### What to Expect

**Test Mode (ENVIRONMENT=test):**
- Processes 50 HubSpot records
- Takes ~2-3 minutes
- Cost: ~$0.001
- Use for: Testing changes, debugging, verification

**Production Mode (ENVIRONMENT=production):**
- Processes all 16,252 HubSpot records
- Takes ~20-30 minutes
- Cost: ~$0.10-0.15
- Use for: Full data matching runs

**During execution you'll see:**
1. Progress bars for data extraction and cleaning
2. Embedding generation progress with cost tracking
3. FAISS partition building (75 partitions by date+state+election_type)
4. High-speed matching with concurrent LLM validation (1500 workers)
5. Final statistics: match rate, confidence distribution, cost breakdown

**Output files created:**
- `offline_data/hubspot_companies_*.parquet` - Intermediate data files
- `output/hubspot_googlesheets_race_matches_latest.parquet` - Latest matches (always updated)
- `output/hubspot_googlesheets_race_matches_YYYYMMDD_HHMMSS.tsv` - Timestamped backup

## Pipeline Steps

### Step 1: Data Extraction

```bash
uv run data_extraction.py
```

Extracts raw data from sources:
- **HubSpot**: 37,986 companies from `stg_airbyte_source__hubspot_api_companies`
- **Google Sheets**: 4,430 races from "Restructured Data" tab

**Output**: `offline_data/hubspot_companies_raw_latest.parquet`, `google_sheets_races_raw_latest.parquet`

### Step 2: Data Cleaning

```bash
uv run data_cleaning.py
```

Cleans and standardizes both datasets:
- Normalize election types (pattern matching)
- Expand HubSpot records (primary/general/runoff dates)
- Parse Excel dates from Google Sheets
- Standardize state codes

**Results**:
- HubSpot: 37,986 → 38,609 (election expansion)
- Google Sheets: 4,430 → 4,404 (cleaned)

**Output**: `offline_data/hubspot_companies_cleaned_latest.parquet`, `google_sheets_races_cleaned_latest.parquet`

### Step 3: Temporal Filtering

```bash
uv run temporal_filtering.py
```

Filters HubSpot to only dates present in Google Sheets:
- Prevents matching candidates from elections with no Google Sheets races
- Reduces dataset from 38,609 → 16,252

**Output**: `offline_data/hubspot_companies_filtered_latest.parquet`

### Step 4: Embedding Generation

```bash
# Test mode (50 HubSpot records)
ENVIRONMENT=test BATCH_SIZE=150 MAX_WORKERS=400 uv run generate_embeddings.py

# Production mode (all records)
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run generate_embeddings.py
```

Generates semantic embeddings for race/office matching:
- **HubSpot**: Pure office name embeddings
- **Google Sheets**: Race name embeddings
- Uses Gemini text-embedding-004 model

**Cost**: ~$0.006 for full dataset

**Output**: `offline_data/hubspot_companies_with_embeddings_latest.parquet`, `google_sheets_races_with_embeddings_latest.parquet`

### Step 5: Production Matching

```bash
# Test mode (50 test records)
ENVIRONMENT=test BATCH_SIZE=1000 MAX_WORKERS=1500 uv run parallel_production_matcher.py

# Production mode (all records)
ENVIRONMENT=production BATCH_SIZE=1000 MAX_WORKERS=1500 uv run parallel_production_matcher.py
```

Executes high-performance matching with:
- **75 FAISS partitions** (date + state + election type)
- **Semantic search** (top 5 candidates per HubSpot record)
- **LLM validation** (70% confidence threshold)
- **1500 concurrent workers** for maximum throughput

**Output**: `output/hubspot_googlesheets_race_matches_latest.parquet`, `hubspot_googlesheets_race_matches_{timestamp}.tsv`

## Expected Results

### Match Quality

The pipeline uses **strict exact matching rules** to prevent false positives:

- **High precision**: Only matches when ALL components are identical (city name, office type, district numbers, directional qualifiers)
- **Conservative matching**: Returns no match rather than "close enough" matches
- **Expected match rate**: Varies by data overlap between HubSpot and Google Sheets
  - Typical range: 5-15% due to different election coverage
  - Higher match rates indicate better data alignment

### Example Valid Matches

Only these types of matches are accepted:

| HubSpot Office | Matched Race | Why Valid |
|----------------|--------------|-----------|
| Pasadena City Council District G | Pasadena City Council District G | Exact city, office, district match |
| USD 249 Board of Education Position 3 | USD 249 Board of Education Position 3 | Exact USD number and position |
| Seattle City Council North Ward 2 | Seattle City Council North Ward 2 | Exact city, direction, ward |

### Example Rejected Matches

These will NOT match (by design):

| HubSpot Office | Candidate Race | Why Rejected |
|----------------|----------------|--------------|
| Pine Hill Borough Council | Gibbsboro Borough Council | Different municipalities |
| USD 249 Board Position 3 | USD 315 Board Position 3 | Different USD numbers (249 ≠ 315) |
| Aberdeen City Council Ward 2 Position 4 | Aberdeen City Council Ward 6 Position 11 | Different ward/position numbers |
| Bainbridge Island Council North Ward | Bainbridge Island Council South Ward 3 | Different directional qualifiers |

### Common No-Match Reasons

1. **No partition found** (~80% of non-matches)
   - Date+State+Election Type combination doesn't exist in Google Sheets
   - HubSpot election not covered in Google Sheets data

2. **Office type mismatch** (~15% of non-matches)
   - Specialized positions not in Google Sheets (e.g., Township Clerk, Water Board, Library Board)

3. **Low confidence / Component mismatch** (~5% of non-matches)
   - Similar but not identical names (different cities, districts, wards)
   - LLM correctly rejects partial matches per strict rules

## Performance

- **FAISS partition building**: ~0.1 seconds for 75 partitions
- **Matching throughput**: ~17 records/second with 1500 workers
- **Total pipeline time**: ~5 minutes for 50 records (including LLM calls)

## Output Schema

Match results include:

```python
{
    'hubspot_company_id': str,
    'candidate_name': str,
    'candidate_office': str,
    'state': str,
    'city': str,  # HubSpot city field
    'district': str,  # HubSpot district field
    'election_date': str,
    'election_type': str,
    'matched_race_id': int | None,
    'matched_race_name': str | None,
    'match_confidence': float,
    'match_reasoning': str,
    'partition_key': str,
    'candidate_races_considered': str  # Top 5 races evaluated (pipe-separated)
}
```

## Data Sources

### HubSpot Companies Table

**Table**: `stg_airbyte_source__hubspot_api_companies`

**Key Fields**:
- `id` (company_id)
- `properties_candidate_office`
- `properties_official_office_name`
- `properties_state`
- `properties_city`
- `properties_candidate_district`
- `properties_election_date`
- `properties_primary_date`
- `properties_runoff_date`

### Google Sheets

**Spreadsheet ID**: `1SnTjTOWjl-m694DZY0TA2ZplYKY_J6m-lyYhhsu_vNs`
**Tab**: "Restructured Data"

**Columns**:
- `race_id` (empty - to be filled)
- `date` (Excel serial format)
- `election_type` (raw state-specific format)
- `race_name` (with state prefix)

## Logs

All logs are written to `logs/__main__.log` with color-coded output:
- `INFO` - Green
- `DEBUG` - Blue (development mode only)
- `ERROR` - Red

## Troubleshooting

### Google OAuth Browser Not Opening

If the browser doesn't open for Google OAuth on first run:
1. Check that port 8080-8090 are available
2. Manually visit the URL shown in the terminal
3. Complete OAuth flow and credentials will be saved to `token.pickle`

### Missing Environment Variables

If you see errors about missing API keys:
1. Verify `.env` file exists at `/Users/collinpark/work/gp-ai-projects/.env`
2. Check all required variables are set (see Prerequisites section)
3. Restart terminal to reload environment

### Databricks Connection Errors

If data extraction fails:
1. Verify `DATABRICKS_API_KEY` is valid
2. Check `DATABRICKS_SERVER_HOSTNAME` format (e.g., `dbc-xxx.cloud.databricks.com`)
3. Verify `DATABRICKS_HTTP_PATH` points to an active SQL warehouse

### Out of Memory Errors

If the pipeline crashes during production matching:
1. Reduce `MAX_WORKERS` (default 1500): `MAX_WORKERS=800 uv run run_full_pipeline.py`
2. Reduce `BATCH_SIZE` (default 1000): `BATCH_SIZE=500 uv run run_full_pipeline.py`
3. Run individual steps separately instead of full pipeline

### Low Match Rate

If matches are unexpectedly low:
1. Check `logs/__main__.log` for "No partition found" messages
2. Review `candidate_races_considered` column to see what races were evaluated
3. Verify Google Sheets data covers the same election dates as HubSpot
4. Check LLM `match_reasoning` field for why matches were rejected

## Usage & Analysis

### Running the Pipeline

The pipeline is ready to use:
```bash
# Test run (50 records, ~2-3 minutes)
ENVIRONMENT=test uv run run_full_pipeline.py

# Production run (16,252 records, ~20-30 minutes)
ENVIRONMENT=production uv run run_full_pipeline.py
```

### Analyzing Results

After running, analyze match quality using the output files:

```python
import pandas as pd

# Load results
matches = pd.read_parquet('output/hubspot_googlesheets_race_matches_latest.parquet')

# Overall statistics
total = len(matches)
matched = len(matches[matches['matched_race_id'].notna()])
print(f"Match rate: {matched/total*100:.1f}% ({matched:,} / {total:,})")

# Confidence distribution
print(matches[matches['matched_race_id'].notna()]['match_confidence'].describe())

# Review no-match reasons
no_matches = matches[matches['matched_race_id'].isna()]
print(no_matches['match_reasoning'].value_counts())

# Check races considered for debugging
print(matches['candidate_races_considered'].head())
```

### Future Enhancements

Potential next steps for this pipeline:

1. **Write matches back to Google Sheets** - Update `race_id` column with matched values
2. **Add confidence-based filtering** - Allow users to review matches below certain confidence thresholds
3. **Improve match rate** - Continue refining LLM prompt based on false positive/negative analysis
4. **Add manual review workflow** - Flag edge cases for human verification
5. **Performance optimization** - Further tune concurrency settings for faster matching
