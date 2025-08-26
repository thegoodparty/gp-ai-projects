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
- Creates labeled format: `"name: John Smith | race: CA Governor"`
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
**Purpose**: Perform high-performance candidate matching with **date-partitioned FAISS optimization**
- **🚀 Date-Partitioned FAISS**: Only searches candidates from relevant election dates
- **Pre-built Indices**: All FAISS indices built upfront for maximum concurrency
- **Data Sorting**: HubSpot data sorted by election date for optimal processing
- LLM validation with strict matching rules to prevent false positives
- Advanced parallelization with ThreadPoolExecutor (up to 2000 workers)
- 70% minimum confidence threshold for match acceptance

**Run Command**:
```bash
# Development mode (200 test records)
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py

# Production mode (all records)
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

**Matching Logic**:
1. **Date-Partitioned Search**: Only search DDHQ candidates from same election dates
2. **Lazy-Load FAISS Index**: Build/cache indices on-demand per election date
3. **Semantic Similarity**: Find top 5 most similar candidates (reduced from 10)
4. **LLM Validation** with strict rules:
   - Name matching (exact or clear variants)
   - Geographic validation (same state)
   - Gender mismatch detection
   - Confidence calibration

**Performance Optimizations**:
- **Targeted Search**: Search only relevant election date partitions instead of full dataset
- **Pre-built Indices**: All date-specific FAISS indices built upfront
- **Parallel Processing**: Up to 2000 concurrent workers for maximum throughput
- **Memory Optimization**: Separate smaller indices per election date

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
# Recommended settings for date-partitioned FAISS optimization
ENVIRONMENT=development          # or 'production'
BATCH_SIZE=1000                 # Records per batch (increased for better performance)
MAX_WORKERS=2000                # Concurrent workers (increased for date partitioning)
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

# Step 5: Run production matching (with date-partitioned FAISS)
ENVIRONMENT=development BATCH_SIZE=1000 MAX_WORKERS=2000 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

### 🧪 **Testing Commands**
```bash
# Test embeddings with limited records
uv run hubspot_ddhq_match/generate_cleaned_embeddings.py --test-rows 100
```

### 🏭 **Production Commands (Full Dataset)**
```bash
# Steps 1-3 remain the same
uv run hubspot_ddhq_match/data_extraction.py
uv run hubspot_ddhq_match/data_cleaning.py
uv run hubspot_ddhq_match/temporal_filtering.py

# Step 4: Full dataset embeddings
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/generate_cleaned_embeddings.py

# Step 5: Full dataset matching (with date-partitioned FAISS)
ENVIRONMENT=production BATCH_SIZE=1000 MAX_WORKERS=2000 uv run hubspot_ddhq_match/parallel_production_matcher.py
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

**Step 5: Production Matching (Date-Partitioned FAISS)**
```bash
ENVIRONMENT=development BATCH_SIZE=1000 MAX_WORKERS=2000 uv run hubspot_ddhq_match/parallel_production_matcher.py
```

**Complete Pipeline (Optional)**
```bash
ENVIRONMENT=development BATCH_SIZE=150 MAX_WORKERS=400 uv run hubspot_ddhq_match/hubspot_ddhq_pipeline.py
```

### ⚠️ **Important Notes**
- Steps 1-3 don't need environment variables (no LLM/parallel processing)
- Step 4 (embeddings): `BATCH_SIZE=150 MAX_WORKERS=400`
- **Step 5 (matching): `BATCH_SIZE=1000 MAX_WORKERS=2000`** ← **Date-partitioned optimization**
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


## Performance Notes

### 🚀 **Date-Partitioned FAISS Optimization**
- **Smart Search**: Only searches candidates from relevant election dates
- **Pre-built Indices**: All FAISS indices built upfront for maximum speed
- **Memory Efficiency**: Separate indices per election date
- **Speed Gains**: Faster for small elections by avoiding full dataset search

### Processing Modes
- **Development Mode**: Processes 200 test records for quick iteration
- **Production Mode**: Processes full dataset (potentially 50K+ records)

### Configuration
- **Embeddings (Step 4)**: `BATCH_SIZE=150 MAX_WORKERS=400`
- **Matching (Step 5)**: `BATCH_SIZE=1000 MAX_WORKERS=2000` ← Optimized for date partitioning
- **Cost Tracking**: Built-in LLM cost monitoring throughout pipeline

## Key Features

✅ **Election Type Awareness**: Separate primary/general matching  
✅ **Temporal Alignment**: Only match candidates from same election dates  
✅ **🚀 Date-Partitioned FAISS**: Smart search only in relevant date partitions  
✅ **Pre-built Indices**: All FAISS indices built upfront for maximum speed  
✅ **Semantic Similarity**: AI-powered candidate name/race matching  
✅ **Quality Controls**: Strict validation to prevent false positives  
✅ **Ultra-High Performance**: 2000+ concurrent workers with optimized batch processing  
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

# Step 5: Run production matching (Date-Partitioned FAISS Optimization)
ENVIRONMENT=development BATCH_SIZE=1000 MAX_WORKERS=2000 uv run hubspot_ddhq_match/parallel_production_matcher.py