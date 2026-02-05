# Golden Data Production Matcher

BR (Ballot Ready) to L2 district matching system for generating golden data.

## Directory Structure

```
prod_gold_data/
├── output/                    # Match results
│   ├── full_state_matching_*.parquet  # Per-state results
│   └── full_state_matching.parquet    # Combined (run merge script)
├── vector_store/              # L2 embeddings by state
│   └── l2_embeddings_*.pkl
├── production_matcher.py      # Main matching script
└── vector_store_generator.py  # Generate L2 embeddings
```

## Running the Matcher

### Single State
```bash
uv run stitch_golden_data/prod_gold_data/production_matcher.py TX
```

### All States
```bash
uv run stitch_golden_data/prod_gold_data/production_matcher.py all_states --max-workers 1500 --max-concurrent-states 2
```

## Merging State Files

After running `all_states`, individual state parquet files are saved separately. To create a single combined file:

```bash
uv run stitch_golden_data/merge_all_states.py
```

This creates `output/full_state_matching.parquet` (~73 MB, 283K records).

## Output Schema

| Column | Description |
|--------|-------------|
| `name` | BR position name |
| `id` | BR position ID |
| `br_database_id` | BR database ID |
| `state` | State code |
| `l2_district_name` | Matched L2 district name |
| `l2_district_type` | Matched L2 district type |
| `is_matched` | Whether a match was found |
| `llm_reason` | LLM reasoning for match decision |
| `confidence` | Match confidence (0-100) |
| `embeddings` | Top embedding candidates considered |
| `top_embedding_score` | Highest embedding similarity score |

## Statistics

- Total records: 283,821
- Match rate: 95.8%
- Average confidence: 98.9%
- False positive rate: 0.01% (27 records)
