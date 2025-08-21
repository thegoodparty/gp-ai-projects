# API Race Details Processor

This script processes candidates from the parquet file and fetches race details from the GoodParty API for each candidate.

## Setup

Ensure your `.env` file contains:
```
GOODPARTY_API_TOKEN=your_jwt_token_here
```

## Usage

### Process All Candidates
```bash
uv run silver-to-gold-migration/api_race_details_processor.py silver-to-gold-migration/offline_data/gp_candidates_needing_rescoring_full.parquet --output silver-to-gold-migration/offline_data/gp_candidates_with_race_details.parquet
```

### Process First 5 Candidates (Testing)
```bash
uv run silver-to-gold-migration/api_race_details_processor.py silver-to-gold-migration/offline_data/gp_candidates_needing_rescoring_full.parquet --limit 5 --output silver-to-gold-migration/offline_data/test_first_5_with_responses.parquet
```

### Resume from Specific Index
```bash
uv run silver-to-gold-migration/api_race_details_processor.py silver-to-gold-migration/offline_data/gp_candidates_needing_rescoring_full.parquet --start-index 100 --output silver-to-gold-migration/offline_data/gp_candidates_with_race_details.parquet
```

## Features

- **Rate Limiting**: 5-second delays between API requests
- **Progress Saving**: Saves progress every 10 requests
- **Resume Capability**: Skips already processed candidates
- **Error Handling**: Captures both successful and failed requests
- **Multiple Formats**: Outputs both `.parquet` and `.csv` files

## Output

The script adds these columns to each row:
- `api_status_code`: HTTP status code from API
- `api_response_data`: JSON response data
- `api_success`: Boolean indicating success/failure
- `api_error`: Error message if request failed

## Runtime

- Full processing (1,250 candidates): ~1.75 hours
- Progress files saved every 10 requests to avoid data loss