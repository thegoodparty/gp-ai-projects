# GP AI Projects

This repository contains AI-powered tools for political campaign planning, data analysis, and voter data matching.

## Projects Overview

### 1. AI Generated Campaign Plan (`ai_generated_campaign_plan/`)
Generates comprehensive political campaign plans using Gemini LLM with automatic cost tracking, web search integration, and parallel section generation.

**Key Features:**
- Complete campaign plan orchestration with 6 sections
- Web search integration via Tavily API
- Parallel processing with cost tracking
- PDF generation and structured data models

### 2. Data Stitching & Analysis (`stitch_golden_data/`)
Data analysis and matching tools for political district matching and voter data processing.

**Key Features:**
- Production-grade matching algorithms in `prod_gold_data/`
- Vector embeddings and similarity search
- Comprehensive data exploration and analysis
- Batch processing with parallel execution
- Output generation in multiple formats

## Setup

### Prerequisites
Make sure you have `uv` installed for Python package management:

```bash
# Install uv if you haven't already
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Environment Setup
```bash
# Set up Python environment
uv sync
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

### Required API Keys
Create a `.env` file in the project root:

```bash
# Primary LLM Provider (Required)
GEMINI_API_KEY=your_gemini_api_key

# Secondary LLM Provider (Optional - fallback)
GEMINI_API_KEY2=your_second_gemini_key_optional

# Web Search (Required for campaign planning)
TAVILY_API_KEY=your_tavily_api_key

# Databricks (Required for data analysis)
DATABRICKS_API_KEY=your_databricks_api_key
DATABRICKS_SERVER_HOSTNAME=your-workspace.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/your-warehouse-id
```

## Running the Projects

### 🎯 Campaign Plan Generation

**Test individual sections:**
```bash
uv run ai_generated_campaign_plan/sections/one_overview.py
uv run ai_generated_campaign_plan/sections/five_know_your_community.py
```

**Generate complete campaign plan:**
```bash
uv run ai_generated_campaign_plan/orchestrator.py
```

### 🔗 Data Analysis & Matching

**Run production matching:**
```bash
uv run stitch_golden_data/prod_gold_data/production_matcher.py
```

**Explore data:**
```bash
uv run stitch_golden_data/merge_all_states.py
```

**Output:** Results saved to `stitch_golden_data/output/` and `stitch_golden_data/prod_gold_data/output/`

## Debug Mode & Logging

### Enable Debug Logging
Set the `ENVIRONMENT` variable to see detailed debug logs:

```bash
# Enable debug logging for any script
 ENVIRONMENT=development   uv run stitch_golden_data/prod_gold_data/production_matcher.py  all_states --batch-size 150 --max-concurrent-states 1   
 ENVIRONMENT=development uv run ai_generated_campaign_plan/orchestrator.py
```

### Log Levels
- **Production** (default): INFO level and above
- **Development** (`ENVIRONMENT=development`): DEBUG level with colored output and detailed tracing

## Project Structure

```
gp-ai-projects/
├── ai_generated_campaign_plan/     # Campaign planning system
│   ├── orchestrator.py            # Main orchestrator
│   ├── sections/                  # Individual plan sections
│   ├── schema/                    # Data models
│   └── utils/                     # Campaign utilities
├── stitch_golden_data/            # Data analysis & matching
│   ├── prod_gold_data/            # Production matching algorithms
│   ├── merge_all_states.py        # State data merging
│   ├── offline_data/              # Cached data files
│   ├── vector_store/              # Embedding storage
│   └── output/                    # Generated results
├── shared/                        # Shared libraries
│   ├── llm.py                     # LLM client with fallback
│   ├── llm_gemini.py             # Gemini-specific client
│   ├── logger.py                  # Environment-aware logging
│   ├── tavily_client.py          # Web search integration
│   └── databricks_client.py      # Databricks connector
└── README.md                      # This file
```

## Key Dependencies

**Core Libraries:**
- **LLM Integration**: `google-genai`, `openai`
- **Data Processing**: `pandas`, `pyarrow`, `numpy`
- **Database**: `databricks-sql-connector`
- **Web Search**: `tavily-python`
- **Async Processing**: `asyncio`, `httpx`
- **Vector Embeddings**: `google-genai` embeddings

### Package Management
```bash
# Add new dependencies
uv add package-name

# Add development dependencies  
uv add --dev package-name

# Remove dependencies
uv remove package-name

# Update all dependencies
uv sync
```

## Cost Tracking

All projects include comprehensive cost tracking:
- **Token usage** across LLM providers
- **Embedding generation** costs
- **API call** summaries
- **Per-operation** breakdowns

Cost summaries are automatically logged and included in output files.

## Important Notes

- **Databricks Access**: All database operations are READ-ONLY (SELECT queries only)
- **API Key Rotation**: Supports multiple Gemini API keys for rate limit management
- **Batch Processing**: Optimized for large-scale data processing with parallel execution