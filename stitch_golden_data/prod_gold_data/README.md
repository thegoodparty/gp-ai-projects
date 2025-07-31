# Production Gold Data Generation

This directory contains the production-ready components for generating BR-L2 district matching data at scale.

## Components

### Core Files
- **`vector_store_generator.py`** - Generate embeddings for all 50 states + DC (51 total jurisdictions)
- **`production_matcher.py`** - Production BR-L2 matching using pre-built vector stores
- **`orchestrator.py`** - Coordinates the complete pipeline (vector generation + matching)
- **`cost_tracker.py`** - Cost tracking utilities for monitoring embedding and LLM usage

### Key Features
- **Complete Coverage**: Processes all 124,266 districts across 51 jurisdictions
- **Optimized Storage**: Simplified text format saves ~40MB compared to previous version
- **Cost Tracking**: Built-in cost monitoring for embeddings and LLM usage
- **Parallel Processing**: Efficient batch processing with rate limiting
- **Production Ready**: Error handling, logging, and resumable operations
- **Isolated Vector Storage**: Vector stores saved in dedicated prod directory regardless of execution location

## Usage

### 1. Generate Vector Stores (One-time Setup)
```bash
# Generate embeddings for all 51 jurisdictions
uv run stitch_golden_data/prod_gold_data/vector_store_generator.py

# Or generate specific states only
python -c "
import asyncio
from stitch_golden_data.prod_gold_data import VectorStoreGenerator
async def main():
    gen = VectorStoreGenerator()
    await gen.generate_all_states(states_to_process=['CA', 'TX', 'NY'])
asyncio.run(main())
"
```

### 2. Run Production Matching
```bash
# Run matching on sample data
uv run stitch_golden_data/prod_gold_data/production_matcher.py

# Run complete pipeline
uv run stitch_golden_data/prod_gold_data/orchestrator.py
```


## Output Structure

### Vector Store Files
```
stitch_golden_data/prod_gold_data/vector_store/
├── l2_embeddings_ca.pkl      # California districts
├── l2_embeddings_tx.pkl      # Texas districts  
├── l2_embeddings_dc.pkl      # Washington D.C. districts
└── ...                       # All 51 jurisdictions
```

### Cached Data Files
```
stitch_golden_data/offline_data/
├── l2_districts_ca.parquet   # California raw data
├── l2_districts_tx.parquet   # Texas raw data
└── ...                       # Cached state data
```

### Output Results
```
stitch_golden_data/output/
├── production_matching_results.tsv    # Main results
├── cost_summary.json                  # Cost breakdown
└── processing_logs/                   # Detailed logs
```

## Data Format

### Text Format (Simplified)
```
"state: CA, district type: City, district name: Los Angeles"
```

### Metadata Structure
```python
{
    'district_name': 'Los Angeles',
    'district_type': 'City',
    'state': 'CA'
}
```

## Performance

- **Total Districts**: 124,266 across 51 jurisdictions
- **Estimated Vector Generation Cost**: ~$30-50 (one-time)
- **Estimated Matching Cost**: ~$0.10 per 1000 BR records
- **Storage Requirements**: ~2GB for all vector stores
- **Processing Time**: ~2-4 hours for complete vector generation

## Requirements

- Python 3.11+
- `uv` package manager
- Required API keys in `.env`:
  - `GEMINI_API_KEY`
  - `DATABRICKS_API_KEY`
  - `DATABRICKS_SERVER_HOSTNAME`
  - `DATABRICKS_HTTP_PATH`