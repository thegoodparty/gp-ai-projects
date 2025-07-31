# BR-L2 District Matching Production System

This directory contains a production-ready system for matching Ballot Ready (BR) political positions to L2 voter data districts using embeddings and LLM selection.

## Directory Structure

- **`prod_gold_data/`** - Production-ready components for generating gold standard data
- **`L2_BR_MATCH_LA_POC.py`** - Louisiana proof-of-concept and testing framework  
- **`offline_data/`** - Cached data files and test results
- **`vector_store/`** - Generated embedding stores
- **`output/`** - Processing results and reports
- **`logs/`** - System logs and debugging information

## System Overview

The system uses a two-phase approach:
1. **Vector Store Generation**: Create semantic embeddings for L2 district data for all 50 states
2. **Production Matching**: Match BR positions to L2 districts using pre-built vector stores + LLM selection

### Output Format

The system produces enhanced datasets with all original BR data plus matching results:

**Added Columns:**
- `l2_district_name` - Matched L2 district name (or error status)
- `l2_district_type` - Matched L2 district type (or error status)
- `is_matched` - Boolean indicating successful match
- `llm_reason` - LLM reasoning for selection/rejection
- `confidence` - LLM confidence score (0-100)
- `top_embedding_score` - Cosine similarity of top embedding match
- `top_embedding_name` - Name of top embedding match
- `alternative_matches` - Other close matches considered

**Output Formats:**
- **Primary**: `.parquet` files with full data and metadata
- **Secondary**: `.tsv` files with human-readable metadata comments

## Key Files

### Core Components
- `vector_store_generator.py` - Generates and manages vector stores for all 50 states
- `production_matcher.py` - Production matcher using pre-built vector stores
- `orchestrator.py` - Main pipeline orchestrator with CLI interface
- `cost_tracker.py` - Comprehensive cost tracking and reporting

### Legacy/PoC Files
- `L2_BR_MATCH_LA_POC.py` - Original Louisiana-only proof of concept
- `embedding_first_llm_second.py` - Alternative implementation (if exists)

## Quick Start

### 1. Generate Vector Stores for All States
```bash
# Generate vector stores for all 50 states (skip existing ones)
uv run stitch_golden_data/orchestrator.py --preset generate_all_vectors

# Force regenerate all vector stores
uv run stitch_golden_data/orchestrator.py --generate-vectors --force-regenerate

# Generate for specific states only
uv run stitch_golden_data/orchestrator.py --generate-vectors --vector-states CA NY TX
```

### 2. Run Production Matching

#### Small Test (Recommended First)
```bash
# Test with 3 states, 50 records
uv run stitch_golden_data/orchestrator.py --preset test_small
```

#### Medium Test
```bash
# Test with 5 states, 500 records
uv run stitch_golden_data/orchestrator.py --preset test_medium
```

#### Production Scale
```bash
# All states, full database
uv run stitch_golden_data/orchestrator.py --preset production_all

# High-volume states only
uv run stitch_golden_data/orchestrator.py --preset production_high_volume_states
```

### 3. Custom Configuration
```bash
# Custom matching run
uv run stitch_golden_data/orchestrator.py \
  --states CA NY TX FL PA \
  --limit 1000 \
  --batch-size 15 \
  --output custom_matching_results.tsv
```

## CLI Usage

### Orchestrator Commands
```bash
# List available preset configurations
uv run stitch_golden_data/orchestrator.py --help

# List available vector stores
uv run stitch_golden_data/orchestrator.py --list-vectors

# Full pipeline: generate vectors + run matching
uv run stitch_golden_data/orchestrator.py --generate-vectors --states CA NY TX --limit 100

# Skip matching, only generate vectors
uv run stitch_golden_data/orchestrator.py --generate-vectors --skip-matching
```

### Cost Tracking Commands
```bash
# Quick cost summary
uv run stitch_golden_data/cost_tracker.py quick

# Detailed cost report
uv run stitch_golden_data/cost_tracker.py report

# Export cost data to CSV
uv run stitch_golden_data/cost_tracker.py export --days 30

# Get summary for specific period
uv run stitch_golden_data/cost_tracker.py summary --days 7
```

### Individual Component Usage
```bash
# Generate vectors only
uv run stitch_golden_data/vector_store_generator.py

# Run matching only (requires existing vectors)
uv run stitch_golden_data/production_matcher.py
```

## Directory Structure

```
stitch_golden_data/
├── README.md                     # This file
├── orchestrator.py              # Main pipeline orchestrator
├── vector_store_generator.py    # Vector store generation
├── production_matcher.py        # Production matching
├── cost_tracker.py             # Cost tracking utilities
├── L2_BR_MATCH_LA_POC.py       # Original PoC (Louisiana only)
├── offline_data/               # Cached data files
│   ├── l2_districts_*.parquet  # L2 data by state
│   └── br_*.parquet           # BR data samples
├── vector_store/              # Pre-built vector stores
│   ├── l2_embeddings_ca.pkl   # California embeddings
│   ├── l2_embeddings_ny.pkl   # New York embeddings
│   └── ...                    # One file per state
├── output/                    # Results and reports
│   ├── *.parquet             # Enhanced BR data with matching results (primary)
│   ├── *.tsv                 # Enhanced BR data with matching results (secondary)
│   └── *.json                # Pipeline metadata
├── cost_tracking/            # Cost tracking data
│   ├── daily_costs.json      # Daily cost summaries
│   ├── detailed_cost_log.jsonl # Detailed cost log
│   └── cost_report_*.txt     # Generated reports
└── logs/                     # Application logs
```

## Preset Configurations

### `generate_all_vectors`
- Generates vector stores for all 50 states
- Skips existing vectors
- Does not run matching

### `test_small`
- 3 states (CA, NY, TX)
- 50 records limit
- Small batch size for testing

### `test_medium`
- 5 states (CA, NY, TX, FL, PA)
- 500 records limit
- Medium batch size

### `production_all`
- All available states
- Full BR database
- Optimized batch sizes

### `production_high_volume_states`
- Top 10 high-volume states
- Full database for those states
- Larger batch sizes

## Cost Management

The system includes comprehensive cost tracking:

- **Real-time tracking**: Costs tracked during execution
- **Daily summaries**: Automated daily cost rollups
- **Detailed logging**: Every API call logged with metadata
- **Projections**: Estimated costs for full database processing
- **Reporting**: Generate detailed cost reports

### Cost Structure
- **Embedding costs**: Google Gemini text-embedding-004 ($0.00001/1K tokens)
- **LLM costs**: Google Gemini Flash ($0.075/1M input, $0.30/1M output)
- **Typical costs**: ~$0.001-0.005 per BR record processed

## Performance Characteristics

### Vector Generation
- **Time**: ~2-5 minutes per state (varies by district count)
- **Cost**: ~$0.50-2.00 per state
- **Storage**: ~1-10MB per state vector store

### Production Matching
- **Throughput**: ~10-20 records/second
- **Cost per record**: ~$0.001-0.005
- **Batch processing**: Configurable parallelism

### Scaling Estimates
- **Full BR database**: ~250K records estimated
- **Total cost**: ~$500-1,500 for complete processing
- **Processing time**: ~3-8 hours for full database

## Error Handling

The system includes comprehensive error handling:
- **Rate limiting**: Automatic backoff for API limits
- **Retries**: Configurable retry logic
- **Partial failures**: Continue processing despite individual failures
- **State isolation**: Failures in one state don't affect others

## Monitoring and Observability

- **Structured logging**: JSON logs with full context
- **Progress tracking**: Real-time progress updates
- **Cost monitoring**: Live cost tracking during execution
- **Results validation**: Automatic result quality checks

## Development Notes

### Key Design Decisions
1. **State-based vector stores**: Each state gets its own vector store for efficient loading
2. **Two-phase matching**: Embedding search + LLM selection for accuracy
3. **Async processing**: Parallel processing for performance
4. **Comprehensive caching**: Avoid reprocessing data and vectors
5. **Cost consciousness**: Track every API call for budget management

### Adding New Features
- Extend `PipelineConfig` for new configuration options
- Add new presets in `create_preset_configs()`
- Implement custom matching logic in `production_matcher.py`
- Add new cost categories in `cost_tracker.py`

### Testing Strategy
1. Start with `test_small` preset
2. Verify results quality and costs
3. Scale to `test_medium`
4. Run production on high-volume states first
5. Full production rollout

## Troubleshooting

### Common Issues

#### "No vector store found for state XX"
```bash
# Generate missing vector store
uv run stitch_golden_data/orchestrator.py --generate-vectors --vector-states XX
```

#### High API costs
```bash
# Check cost breakdown
uv run stitch_golden_data/cost_tracker.py quick

# Reduce batch sizes
uv run stitch_golden_data/orchestrator.py --states CA --batch-size 5 --limit 100
```

#### Rate limiting errors
- Reduce `batch_size` and increase delays in the code
- Check Gemini API quotas and limits

#### Memory issues
- Process fewer states at once
- Clear vector store cache between runs

### Getting Help

1. Check logs in `stitch_golden_data/logs/`
2. Review cost reports for unexpected spending
3. Use smaller test batches to isolate issues
4. Check vector store availability with `--list-vectors`

## Future Enhancements

- **Vector store versioning**: Track embedding model versions
- **A/B testing**: Compare different matching strategies
- **Active learning**: Improve matching based on feedback
- **Real-time matching**: API endpoints for live matching
- **Advanced analytics**: Detailed matching quality metrics