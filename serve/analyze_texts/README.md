# Analyze Texts Pipeline

A modular, high-throughput pipeline for analyzing civic engagement messages from political campaigns.

## Overview

This pipeline processes consolidated campaign message data through 6 well-separated stages:

0. **Data Loading** - Load consolidated CSV with demographics
1. **Data Cleaning** - Normalize text, fix encoding issues
2. **Data Filtering** - Remove STOP messages and emoji starters
3. **Atomization & Anonymization** - Split compound messages, anonymize locations
4. **Classification** - Classify into hierarchical issue taxonomy
5. **Synthesis** - Smart summarization (AI for small categories, clustering for large)

## Features

- ✅ **Row Lineage Tracking**: Track messages through atomization with `original_row_idx` and `atomic_idx`
- ✅ **High-Throughput Processing**: 1200+ concurrent LLM connections using ThreadPoolExecutor
- ✅ **Smart Synthesis**: AI summarization for <20 messages, clustering for larger categories
- ✅ **Demographic Preservation**: All demographics preserved through atomization
- ✅ **Cost Tracking**: Built-in LLM and embedding cost tracking
- ✅ **Modular Design**: Clean separation of concerns across 6 stages

## Directory Structure

```
serve/analyze_texts/
├── models.py                  # Pydantic data models
├── config.yaml               # Pipeline configuration
├── orchestrator.py           # Main pipeline orchestrator
├── stages/
│   ├── stage_0_loader.py     # CSV loading
│   ├── stage_1_cleaner.py    # Text cleaning
│   ├── stage_2_filter.py     # Message filtering
│   ├── stage_3_atomizer.py   # Atomization & anonymization (LLM)
│   ├── stage_4_classifier.py # Classification (LLM)
│   └── stage_5_synthesizer.py # Smart summarization
├── scripts/
│   └── run_pipeline.py       # CLI entry point
└── output/                   # Generated outputs
    └── {campaign}/
        ├── {campaign}_atomized.csv
        ├── {campaign}_category_summaries.json
        ├── {campaign}_analysis_report.md
        └── {campaign}_pipeline_stats.json
```

## Quick Start

### 1. Install Dependencies

```bash
cd gp-ai-projects
uv sync
source .venv/bin/activate
```

### 2. Configure API Keys

Ensure these environment variables are set in `.env`:
```
GEMINI_API_KEY=your_gemini_api_key
```

### 3. Run the Pipeline

```bash
# Standard run
uv run serve/analyze_texts/scripts/run_pipeline.py --campaign berkley

# Debug mode
ENVIRONMENT=development uv run serve/analyze_texts/scripts/run_pipeline.py --campaign berkley

# Skip atomization (use original messages)
uv run serve/analyze_texts/scripts/run_pipeline.py --campaign berkley --no-atomize

# Other campaigns
uv run serve/analyze_texts/scripts/run_pipeline.py --campaign cara-burnsville
uv run serve/analyze_texts/scripts/run_pipeline.py --campaign josh-minooka
```

## Pipeline Stages

### Stage 0: Data Loader

**Input**: `serve/data/{campaign}_consolidated.csv`

**Process**:
- Load consolidated CSV with all demographics
- Convert to MessageRecord objects
- Assign `original_row_idx` and `atomic_idx=0`

**Output**: List[MessageRecord]

### Stage 1: Data Cleaner

**Process**:
- Normalize whitespace (preserve line breaks)
- Fix encoding issues (zero-width chars, quotes)
- Remove text artifacts

**Output**: List[MessageRecord] with cleaned text

### Stage 2: Data Filter

**Filter Rules**:
- STOP messages (STOP, UNSUBSCRIBE, etc.)
- Messages starting with emoji

**Output**: Substantive messages + filter statistics

### Stage 3: Atomizer & Anonymizer

**Atomization**:
- Use LLM to detect compound messages
- Example: "taxes too high, roads have potholes" → 2 atomic messages
- Preserve ALL demographics for each atomic part
- Track lineage: `original_row_idx` + `atomic_idx`

**Anonymization**:
- Replace campaign-specific terms (e.g., "Berkley" → "the local area")
- Configurable rules in config.yaml

**Performance**: 1200 concurrent connections, 400 workers

### Stage 4: Classifier

**Taxonomy**: 8 primary categories, 2-3 secondary each
- infrastructure_and_transportation
- public_safety
- education
- housing_and_development
- health_and_human_services
- economic_development
- quality_of_life
- government_operations

**Classification Output**:
- primary_category
- secondary_category
- stance (positive, negative, neutral, requesting)
- specific_concern

**Performance**: 1200 concurrent connections, 400 workers

### Stage 5: Synthesizer

**Strategy**:

**For categories with <20 messages**: Direct AI summarization
- Single LLM call per category
- Extract themes, quotes, action items

**For categories with ≥20 messages**: Cluster then summarize
1. Generate embeddings (GeminiEmbeddingClient)
2. Cluster with HDBSCAN (semantic grouping)
3. Parallel cluster analysis
4. Aggregate cluster summaries

**Output**: Category summaries with themes, quotes, sentiment

## Configuration

Edit `serve/analyze_texts/config.yaml`:

```yaml
pipeline:
  campaign: "berkley"  # Default campaign

atomizer:
  enabled: true
  anonymize: true
  anonymization_rules:
    berkley: "the local area"
    burnsville: "the local area"
  llm_config:
    max_connections: 1200
    max_workers: 400

synthesizer:
  small_category_threshold: 20  # Threshold for AI vs clustering
  clustering:
    enabled: true
    min_cluster_size: 5
```

## Output Files

### 1. Atomized CSV (`{campaign}_atomized.csv`)

All atomized messages with:
- Row lineage (original_row_idx, atomic_idx)
- Full demographics
- Classification results
- Stance information

### 2. Category Summaries JSON (`{campaign}_category_summaries.json`)

```json
[
  {
    "primary_category": "infrastructure_and_transportation",
    "secondary_category": "roads_and_bridges",
    "message_count": 45,
    "unique_respondents": 38,
    "method": "cluster_summary",
    "summary": "...",
    "key_themes": [...],
    "verbatim_quotes": [...],
    "action_items": [...],
    "sentiment_distribution": {...}
  }
]
```

### 3. Analysis Report (`{campaign}_analysis_report.md`)

Human-readable markdown report with:
- Executive summary
- Category-by-category breakdowns
- Key themes and quotes
- Sentiment analysis

### 4. Pipeline Stats (`{campaign}_pipeline_stats.json`)

Timing and cost information:
- Stage durations
- Message flow statistics
- LLM usage and costs

## Performance

- **Atomization**: 1200 concurrent LLM connections
- **Classification**: 1200 concurrent LLM connections
- **Throughput**: ~10,000+ messages/minute classification
- **Cost Efficiency**: thinking_budget=0 for Flash model ($0.075/1M tokens)

## Data Models

### MessageRecord
```python
original_row_idx: int      # Original CSV row
atomic_idx: int            # 0 for original, 1+ for atomized
phone_number: str
message_text: str
campaign_source: str
round: str
# All demographics preserved
voters_age, voters_gender, age_group, etc.
```

### ClassifiedMessage
```python
message: MessageRecord
classification: IssueClassification
  - primary_category
  - secondary_category
  - stance
  - specific_concern
```

### CategorySummary
```python
primary_category: str
secondary_category: str
message_count: int
unique_respondents: int
method: str  # "ai_summary" or "cluster_summary"
summary: str
key_themes: List[str]
verbatim_quotes: List[str]
action_items: List[str]
sentiment_distribution: Dict[str, int]
```

## Examples

### Example 1: Analyze Berkley Campaign

```bash
uv run serve/analyze_texts/scripts/run_pipeline.py --campaign berkley
```

Output:
```
=== STAGE 0: DATA LOADING ===
Loaded 175 messages from berkley campaign

=== STAGE 1: DATA CLEANING ===
Cleaned 175 messages

=== STAGE 2: DATA FILTERING ===
Filtered out 5 STOP messages, 2 emoji starters
Remaining: 168 substantive messages

=== STAGE 3: ATOMIZATION & ANONYMIZATION ===
Atomization complete: 168 → 203 messages

=== STAGE 4: MESSAGE CLASSIFICATION ===
Classification complete: 203 messages classified

=== STAGE 5: CATEGORY SYNTHESIS ===
Generated 12 category summaries

✅ Pipeline completed successfully!
```

### Example 2: Debug Mode

```bash
ENVIRONMENT=development uv run serve/analyze_texts/scripts/run_pipeline.py --campaign berkley
```

Enables detailed debug logging for troubleshooting.

## Troubleshooting

### Missing consolidated CSV
```
FileNotFoundError: Consolidated CSV not found
```
**Solution**: Run `serve/consolidate_replies_results.py` first to generate consolidated files.

### LLM API Errors
```
Failed to create embeddings after 5 attempts
```
**Solution**: Check GEMINI_API_KEY in .env, reduce max_workers in config.yaml

### Out of Memory
**Solution**: Reduce batch_size and max_concurrent_batches in synthesizer config

## Architecture Notes

- **Separation of Concerns**: Each stage is independent and testable
- **Row Lineage**: original_row_idx + atomic_idx track messages through expansion
- **Parallel Processing**: ThreadPoolExecutor + asyncio.gather() for non-blocking LLM calls
- **Cost Efficiency**: thinking_budget=0 reduces Flash cost 30x ($2.50 → $0.075 per 1M tokens)
- **Error Handling**: Graceful degradation with fallback summaries

## Next Steps

1. Run pipeline on all campaigns
2. Analyze category summaries for insights
3. Build dashboard with demographic filtering
4. Create expandable sections with constituent attribution
