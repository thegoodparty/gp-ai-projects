# HubSpot-DDHQ Candidate Matching System

A sophisticated AI-powered pipeline that matches political candidates between HubSpot (GoodParty's CRM) and DDHQ (Decision Desk HQ election results) databases using semantic embeddings and LLM validation.

## Overview

This system performs **record linkage** between two political databases:
- **HubSpot**: GoodParty's candidate database with campaign information
- **DDHQ**: Decision Desk HQ election results with actual election outcomes

The matching process uses FAISS similarity search on semantic embeddings, followed by LLM validation to ensure high-quality matches with 88%+ confidence threshold.

## Architecture

### Data Flow
```
Raw Data → Cleaning → Election Expansion → Temporal Filtering → Embeddings → Matching
```

Each HubSpot candidate with both primary and general elections is split into **two separate records**:
- One for primary election matching
- One for general election matching

This ensures precise matching between candidates and their specific election results.

## Scripts Overview

### 1. Data Extraction (`data_extraction.py`)
**Purpose**: Extract raw data from Databricks tables
- Pulls HubSpot candidacy data from `dbt.m_general__candidacy`
- Pulls DDHQ election results from `dbt.stg_airbyte_source__ddhq_gdrive_election_results`
- Saves both as parquet and TSV files in `offline_data/`

**Run Command**:
```bash
uv run hubspot_ddhq_match/data_extraction.py
```

**Output Files**:
- `offline_data/hubspot_candidacy_latest.parquet`
- `offline_data/ddhq_election_results_latest.parquet`

---

### 2. Data Cleaning (`data_cleaning.py`)
**Purpose**: Clean and standardize both datasets + expand elections
- Standardizes name formatting (proper case, removes corruption)
- Normalizes geographic information (state codes)
- Cleans office/race names
- **NEW**: Expands candidates into separate primary/general records
- Removes data quality issues

**Run Command**:
```bash
uv run hubspot_ddhq_match/data_cleaning.py
```

**Key Features**:
- Each candidate with both primary and general elections becomes 2 records
- Adds `election_type` field ('primary' or 'general')
- Adds unified `election_date` field

**Output Files**:
- `offline_data/hubspot_candidacy_cleaned_latest.parquet`
- `offline_data/ddhq_election_results_cleaned_latest.parquet`

---

### 3. Temporal Filtering (`temporal_filtering.py`)
**Purpose**: Filter HubSpot to only candidates with election dates matching DDHQ
- Ensures we only match candidates from actual elections that DDHQ has results for
- Uses exact date matching between HubSpot `election_date` and DDHQ `date`
- Prevents matching 2020 candidates with 2024 results

**Run Command**:
```bash
uv run hubspot_ddhq_match/temporal_filtering.py
```

**Output Files**:
- `offline_data/hubspot_filtered_to_match_ddhq_dates_latest.parquet`

---

### 4. Embedding Generation (`generate_cleaned_embeddings.py`)
**Purpose**: Create semantic embeddings for improved matching
- Uses Google Gemini to generate embeddings for candidate names + race information
- Creates labeled format: `"name: John Smith | race: CA Governor | election: primary"`
- Enables semantic similarity search with FAISS

**Run Command**:
```bash
# Development mode (debug logging, conservative batching)
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py

# Test mode (limit records)
uv run hubspot_ddhq_match/generate_cleaned_embeddings.py --test-rows 100
```

**Environment Variables**:
- `ENVIRONMENT`: `development` (debug) or `production` (info logging)
- `BATCH_SIZE`: Records per embedding batch (default: 50, recommended: 150)
- `MAX_WORKERS`: Concurrent embedding requests (default: 2, recommended: 400)

**Output Files**:
- `offline_data/hubspot_filtered_with_embeddings_latest.parquet`
- `offline_data/ddhq_with_embeddings_cleaned_latest.parquet`

---

### 5. Production Matching (`parallel_production_matcher.py`)
**Purpose**: Perform high-performance candidate matching
- FAISS similarity search to find top 10 most similar candidates
- LLM validation with strict matching rules to prevent false positives
- Advanced parallelization with ThreadPoolExecutor (150+ workers)
- 88% confidence threshold for match acceptance

**Run Command**:
```bash
# Development mode (200 test records)
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py

# Production mode (all records)
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

**Matching Logic**:
1. Semantic similarity search (FAISS)
2. LLM validation with strict rules:
   - Name matching (exact or clear variants)
   - Geographic validation (same state)
   - Gender mismatch detection
   - Confidence calibration

**Output Files**:
- `output/parallel_hubspot_ddhq_matches_latest.parquet`
- `output/parallel_hubspot_ddhq_matches_latest.tsv`

---

### 6. Pipeline Orchestrator (`hubspot_ddhq_pipeline.py`)
**Purpose**: Run all steps in sequence with error handling
- Coordinates steps 1-5 with comprehensive logging
- Environment-aware configuration
- Step skipping options for development
- Cost tracking and performance monitoring

**Run Command**:
```bash
# Full pipeline - development mode
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/hubspot_ddhq_pipeline.py

# Skip specific steps
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/hubspot_ddhq_pipeline.py --skip-extraction --skip-cleaning
```

## Environment Configuration

### Required Environment Variables
```bash
# LLM API Keys
GEMINI_API_KEY=your_gemini_key

# Databricks (for data extraction)
DATABRICKS_API_KEY=your_databricks_key
DATABRICKS_SERVER_HOSTNAME=your_hostname
DATABRICKS_HTTP_PATH=your_http_path
```

### Performance Configuration
```bash
# Recommended settings for optimal performance
ENVIRONMENT=development          # or 'production'
BATCH_SIZE=150                  # Records per batch
MAX_WORKERS=400                 # Concurrent workers
LOG_LEVEL=DEBUG                 # or 'INFO'
```

## Quick Start Commands

### 🚀 **Complete Workflow (Copy-Paste Ready)**

**Run all steps sequentially:**
```bash
# Step 1: Extract data from Databricks
uv run hubspot_ddhq_match/data_extraction.py

# Step 2: Clean data and expand elections (primary/general split)
uv run hubspot_ddhq_match/data_cleaning.py

# Step 3: Filter by temporal alignment
uv run hubspot_ddhq_match/temporal_filtering.py

# Step 4: Generate semantic embeddings
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py

# Step 5: Run production matching
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

### 🧪 **Testing Commands**
```bash
# Test embeddings with limited records
uv run hubspot_ddhq_match/generate_cleaned_embeddings.py --test-rows 100

# Test various embedding formats
uv run hubspot_ddhq_match/test_embedding_formats.py
uv run hubspot_ddhq_match/test_real_data_embedding_formats.py
```

### 🏭 **Production Commands (Full Dataset)**
```bash
# Steps 1-3 remain the same
uv run hubspot_ddhq_match/data_extraction.py
uv run hubspot_ddhq_match/data_cleaning.py
uv run hubspot_ddhq_match/temporal_filtering.py

# Step 4: Full dataset embeddings
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py

# Step 5: Full dataset matching
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

### 📋 **Individual Script Commands**

**Step 1: Data Extraction**
```bash
uv run hubspot_ddhq_match/data_extraction.py
```

**Step 2: Data Cleaning + Election Expansion**
```bash
uv run hubspot_ddhq_match/data_cleaning.py
```

**Step 3: Temporal Filtering**
```bash
uv run hubspot_ddhq_match/temporal_filtering.py
```

**Step 4: Embedding Generation**
```bash
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py
```

**Step 5: Production Matching**
```bash
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

**Complete Pipeline (Optional)**
```bash
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/hubspot_ddhq_pipeline.py
```

### ⚠️ **Important Notes**
- Steps 1-3 don't need environment variables (no LLM/parallel processing)
- Steps 4-5 use performance settings: `BATCH_SIZE=150 MAX_WORKERS=400`
- `ENVIRONMENT=development` processes 200 test records
- `ENVIRONMENT=production` processes the full dataset

## Data Dependencies

```
data_extraction.py → data_cleaning.py → temporal_filtering.py → generate_cleaned_embeddings.py → parallel_production_matcher.py
```

Each script depends on the output files from the previous step:
- Extraction → Raw parquet files
- Cleaning → Cleaned + expanded datasets
- Temporal → Filtered HubSpot dataset
- Embeddings → Datasets with semantic embeddings
- Matching → Final match results

## Output Files Structure

```
hubspot_ddhq_match/
├── offline_data/           # Intermediate data files
│   ├── hubspot_candidacy_latest.parquet
│   ├── ddhq_election_results_latest.parquet
│   ├── hubspot_candidacy_cleaned_latest.parquet
│   ├── ddhq_election_results_cleaned_latest.parquet
│   ├── hubspot_filtered_to_match_ddhq_dates_latest.parquet
│   ├── hubspot_filtered_with_embeddings_latest.parquet
│   └── ddhq_with_embeddings_cleaned_latest.parquet
└── output/                 # Final results
    ├── parallel_hubspot_ddhq_matches_latest.parquet
    └── parallel_hubspot_ddhq_matches_latest.tsv
```

## Testing Scripts

Additional test scripts for format and embedding optimization:
```bash
uv run hubspot_ddhq_match/test_discrimination_formats.py
uv run hubspot_ddhq_match/test_embedding_formats.py
uv run hubspot_ddhq_match/test_focused_discrimination.py
uv run hubspot_ddhq_match/test_format_comparison.py
uv run hubspot_ddhq_match/test_label_variations.py
uv run hubspot_ddhq_match/test_nickname_variations.py
uv run hubspot_ddhq_match/test_real_data_embedding_formats.py
```

## Performance Notes

- **Development Mode**: Processes 200 test records for quick iteration
- **Production Mode**: Processes full dataset (potentially 50K+ records)
- **Batch Size 150**: Optimal balance of throughput and API limits
- **Max Workers 400**: Aggressive parallelization for maximum speed
- **Cost Tracking**: Built-in LLM cost monitoring throughout pipeline

## Key Features

✅ **Election Type Awareness**: Separate primary/general matching  
✅ **Temporal Alignment**: Only match candidates from same election dates  
✅ **Semantic Similarity**: AI-powered candidate name/race matching  
✅ **Quality Controls**: Strict validation to prevent false positives  
✅ **High Performance**: 400+ concurrent workers with batch processing  
✅ **Cost Monitoring**: Real-time LLM usage and cost tracking  
✅ **Environment Flexibility**: Development vs production modes  


📝 Copy-Paste Commands for Terminal (TL:DR; version)

Run all steps sequentially:

# Step 1: Extract data from Databricks
uv run hubspot_ddhq_match/data_extraction.py

# Step 2: Clean data and expand elections (primary/general split)
uv run hubspot_ddhq_match/data_cleaning.py

# Step 3: Filter by temporal alignment
uv run hubspot_ddhq_match/temporal_filtering.py

# Step 4: Generate semantic embeddings
ENVIRONMENT=development BATCH_SIZE=100 MAX_WORKERS=80 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py

# Step 5: Run production matching
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=800 uv run hubspot_ddhq_match/parallel_production_matcher.py
