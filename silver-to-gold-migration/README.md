# Silver to Gold Migration Tool

This tool helps migrate silver tier clients to gold tier by running the scoring model again on candidates that need rescoring.

## Overview

The production matcher connects to Ballot Ready (BR) data at `goodparty_data_catalog.dbt.int__enhanced_position` and this tool pulls candidates from `goodparty_data_catalog.sandbox.gp_candidates_needing_rescoring` for rescoring.

## Setup

Ensure you have the required environment variables set in your `.env` file:

```bash
DATABRICKS_SERVER_HOSTNAME=your-workspace.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/your-warehouse-id
DATABRICKS_API_KEY=your-personal-access-token
```

## Usage

### Basic Usage

```bash
# Pull all candidates needing rescoring
uv run silver-to-gold-migration/pull_rescoring_candidates.py

# Pull with limit
uv run silver-to-gold-migration/pull_rescoring_candidates.py --limit 100

# Analyze table structure only
uv run silver-to-gold-migration/pull_rescoring_candidates.py --analyze
```

### State-Specific Queries

```bash
# Pull candidates for a specific state
uv run silver-to-gold-migration/pull_rescoring_candidates.py --state CA

# Pull limited sample for a state
uv run silver-to-gold-migration/pull_rescoring_candidates.py --state NY --limit 50
```

### Output Formats

```bash
# Save as parquet (default)
uv run silver-to-gold-migration/pull_rescoring_candidates.py --format parquet

# Save as CSV
uv run silver-to-gold-migration/pull_rescoring_candidates.py --format csv

# Save as TSV
uv run silver-to-gold-migration/pull_rescoring_candidates.py --format tsv
```

## Key Features

- **Connection to Databricks**: Uses the shared `DatabricksClient` for secure connections
- **Data Analysis**: Can analyze table structure and provide data summaries
- **Flexible Output**: Supports parquet, CSV, and TSV formats
- **State Filtering**: Can filter candidates by state
- **Error Handling**: Comprehensive error handling and logging
- **Sampling**: Supports limiting the number of rows pulled for testing

## Output Files

Data is saved to the `data/` directory with descriptive filenames:
- `gp_candidates_needing_rescoring_full.parquet` - All candidates
- `gp_candidates_needing_rescoring_sample_100.parquet` - Limited sample
- `gp_candidates_needing_rescoring_ca.parquet` - State-specific data

## Integration with Production Matcher

The production matcher processes BR data from `goodparty_data_catalog.dbt.int__enhanced_position` and this tool focuses on the candidates identified for rescoring in the sandbox schema. The workflow is:

1. **Identify Candidates**: Pull candidates needing rescoring using this tool
2. **Run Scoring Model**: Apply the production matcher or similar scoring logic
3. **Update Tier**: Migrate successful candidates from silver to gold tier

## Next Steps

After pulling the data:
1. Review the candidate data structure
2. Run the scoring/matching model on these candidates
3. Update their tier classification in the database
4. Monitor the success rate and quality of the upgrades