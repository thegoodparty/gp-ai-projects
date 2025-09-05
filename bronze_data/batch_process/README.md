# Batch Processing Pipeline

High-performance data processing pipeline for candidate matching and user motivation analysis using ultra-high throughput LLM configurations.

## Overview

This directory contains bronze-tier data processing scripts that handle large-scale candidate analysis with optimized parallel processing capabilities.

## Scripts

### `parallel_unmatched_user_matcher.py`
**Ultra-high throughput candidate matching with semantic embeddings**

- **Performance**: 1,500 records/minute with 1,200 concurrent connections
- **Cost**: ~$0.04 per 3,000 records using Gemini 2.5 Flash with thinking disabled
- **Features**: FAISS vector similarity search, batch processing, comprehensive error handling

```bash
# High-performance matching with DDHQ proven settings
ENVIRONMENT=development uv run bronze_data/batch_process/parallel_unmatched_user_matcher.py

# Custom configuration
uv run bronze_data/batch_process/parallel_unmatched_user_matcher.py --max-workers 1500 --batch-size 1000
```

### `preprocessing.py`
**Candidate data preprocessing and quality assessment**

- Normalizes candidate information (names, offices, locations)
- Performs seriousness assessment and spam filtering
- Generates clean datasets for downstream processing

```bash
uv run bronze_data/batch_process/preprocessing.py
```

### `create_motivated_users_parquet.py`
**User motivation analysis and parquet generation**

- Analyzes user engagement patterns
- Creates optimized parquet files for analytics
- Integrates with Databricks for data pipeline compatibility

```bash
uv run bronze_data/batch_process/create_motivated_users_parquet.py
```

## Key Performance Patterns

### DDHQ Proven Ultra-High Throughput Settings
```python
# Maximum performance configuration
target_concurrency = 1200  # 10k/min target
llm_client = GeminiClient(
    default_model=GeminiModelType.FLASH,
    default_temperature=0.0,
    thinking_budget=0,  # Cost optimization: $0.075/1M vs $2.50/1M
    max_connections=target_concurrency,
    max_keepalive_connections=300
)

# ThreadPoolExecutor with maximum workers
thread_pool = ThreadPoolExecutor(max_workers=1500)
```

### Batch Processing Optimization
- **Batch Size**: 1,000 records for optimal throughput
- **Connection Limits**: 1,200 max connections, 300 keepalive
- **Model Selection**: Gemini 2.5 Flash with thinking disabled
- **Error Handling**: Comprehensive retry logic with exponential backoff

## Output Structure

```
output/
├── preprocessing_results/     # Clean candidate datasets
├── matching_results/         # Vector similarity matches
└── analytics/               # Performance metrics and logs

logs/
├── processing_YYYYMMDD_HHMMSS.log
└── error_reports/

offline_data/
├── cached_embeddings/       # Vector store cache
└── backup_datasets/         # Data snapshots
```

## Dependencies

- **Vector Stores**: Requires `../../stitch_golden_data/prod_gold_data/vector_store/`
- **Shared Modules**: Uses `../../shared/` for LLM clients and utilities
- **Environment**: Set `ENVIRONMENT=development` for debug mode

### Vector Store Setup

If the vector store directory is empty or not found, you must generate embeddings first:

```bash
# Generate vector store embeddings (required before running matching)
cd ../../stitch_golden_data/prod_gold_data
uv run vector_store_generator.py

# Then return to run batch processing
cd ../../bronze_data/batch_process
uv run parallel_unmatched_user_matcher.py
```

**Important**: The matching pipeline cannot function without pre-generated vector embeddings. Always ensure the vector store is populated before running candidate matching operations.

## Environment Variables

```bash
# Required for LLM operations
GEMINI_API_KEY=your_gemini_key

# Optional for enhanced functionality
DATABRICKS_API_KEY=your_databricks_key
DATABRICKS_SERVER_HOSTNAME=your_workspace.cloud.databricks.com
TAVILY_API_KEY=your_tavily_key
```

## Performance Monitoring

All scripts include built-in performance tracking:
- Token usage and cost estimation
- Processing rates (records/minute)
- Error rates and retry statistics
- Memory usage and connection metrics

Enable debug logging:
```bash
ENVIRONMENT=development uv run bronze_data/batch_process/script_name.py
```