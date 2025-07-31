# GP AI Projects

This repository contains AI-powered tools for political campaign planning, data analysis, and voter data matching.

## Projects Overview

### 1. AI Generated Campaign Plan (`ai_generated_campaign_plan/`)
Generates comprehensive political campaign plans using LLM providers (Gemini → TogetherAI fallback) with automatic cost tracking, web search integration, and parallel section generation.

**Key Features:**
- Complete campaign plan orchestration with 6 sections
- Automatic LLM provider fallback (Gemini primary, TogetherAI secondary)
- Web search integration via Tavily API
- Parallel processing with cost tracking
- PDF generation and structured data models

### 2. L2-BR Political District Matcher (`stitch_golden_data/L2_BR_MATCH_LA_POC.py`)
Louisiana proof-of-concept for matching Ballot Ready political positions to L2 voter districts using a two-step approach: embedding similarity search + LLM selection.

**Key Features:**
- Embedding-first matching (top 10 districts via semantic similarity)
- LLM-powered final district selection with confidence scoring
- Vector storage with automatic embedding generation
- Batch processing with parallel execution
- Comprehensive cost tracking and TSV output

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
TOGETHER_API_KEY=your_together_api_key

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

### 🔗 L2-BR District Matching (Louisiana POC)

**Run the complete matching workflow:**
```bash
uv run stitch_golden_data/L2_BR_MATCH_LA_POC.py
```

**Output:** Results saved to `stitch_golden_data/output/` as TSV files with cost analysis.

## Debug Mode & Logging

### Enable Debug Logging
Set the `ENVIRONMENT` variable to see detailed debug logs:

```bash
# Enable debug logging for any script
ENVIRONMENT=development uv run stitch_golden_data/L2_BR_MATCH_LA_POC.py
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
│   ├── L2_BR_MATCH_LA_POC.py     # L2-BR matching POC (main)
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
- **Fallback Systems**: Automatic provider fallback (Gemini → TogetherAI)
- **Batch Processing**: Optimized for large-scale data processing with parallel execution