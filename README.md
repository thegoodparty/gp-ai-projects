# GP AI Projects

AI-powered tools for political campaign planning, message analysis, and voter data matching at GoodParty.org.

## Projects Overview

### 1. Serve Pipeline (`serve/`)
Production message analysis system deployed on AWS ECS Fargate with Step Functions orchestration.

- **`v1_pipeline/`** - Complete pipeline: message consolidation, classification, clustering, DynamoDB upload
- **`hierarchical_discovery/`** - Multi-cluster analysis with Gemini AI, verbatim quote extraction, action items
- **`classify/`** - High-throughput civic message classification

### 2. HubSpot-DDHQ Matcher (`hubspot_ddhq_match/`)
High-performance semantic matching system linking HubSpot candidates to DDHQ election races.

- City-contextualized embeddings with FAISS indices
- LLM-based federal/state/local race classification
- 10K+ matches/minute with 1500 concurrent workers

### 3. AI Campaign Plan Generator (`ai_generated_campaign_plan/`)
Generates comprehensive political campaign plans using Gemini LLM.

- 6-section campaign plan orchestration
- Web search integration via Tavily API
- Parallel processing with cost tracking

### 4. Infrastructure (`infrastructure/`)
Terraform modules for AWS deployment (ECS Fargate, Step Functions, Lambda triggers, Secrets Manager).

## Setup

### Prerequisites
```bash
# Install uv package manager
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Environment Setup
```bash
uv sync
source .venv/bin/activate
```

### Required API Keys
Create a `.env` file:
```bash
GEMINI_API_KEY=your_gemini_api_key
TAVILY_API_KEY=your_tavily_api_key
DATABRICKS_API_KEY=your_databricks_api_key
DATABRICKS_SERVER_HOSTNAME=your-workspace.cloud.databricks.com
DATABRICKS_HTTP_PATH=/sql/1.0/warehouses/your-warehouse-id
BRAINTRUST_API_KEY=your_braintrust_api_key  # For LLM tracing
```

## Running the Projects

### Serve Pipeline
```bash
# V1 pipeline with test mode
uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley --test

# Debug mode
ENVIRONMENT=development uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley --test
```

### HubSpot-DDHQ Matching
```bash
cd hubspot_ddhq_match/google_sheets_matcher

# Generate embeddings
ENVIRONMENT=production BATCH_SIZE=150 MAX_WORKERS=400 uv run generate_embeddings.py

# Run matching
ENVIRONMENT=production BATCH_SIZE=1000 MAX_WORKERS=1500 uv run parallel_production_matcher.py
```

### Campaign Plan Generation
```bash
# Test individual sections
uv run ai_generated_campaign_plan/sections/one_overview.py

# Generate complete plan
uv run ai_generated_campaign_plan/orchestrator.py
```

## Workspace Structure (Monorepo)

This is a **uv workspace** with multiple packages optimized for minimal Docker image sizes:

```
gp-ai-projects/
├── pyproject.toml                 # Root: ALL deps for local development
├── shared/
│   └── pyproject.toml             # gp-shared: core utilities
├── serve/v1_pipeline/
│   └── pyproject.toml             # gp-v1-pipeline: pipeline-specific deps
└── hubspot_ddhq_match/
    └── pyproject.toml             # gp-ddhq-matcher: matcher-specific deps
```

**Adding dependencies:**
- Shared utilities (llm, logger, braintrust) → `shared/pyproject.toml`
- Pipeline-specific (matplotlib, scipy) → `serve/v1_pipeline/pyproject.toml`
- Matcher-specific (faiss, pyarrow) → `hubspot_ddhq_match/pyproject.toml`

Docker builds use `uv sync --package <name>` to install only required dependencies.

## Project Structure

```
gp-ai-projects/
├── serve/                         # Message analysis pipelines
│   ├── v1_pipeline/              # Main pipeline (Docker-deployed)
│   ├── hierarchical_discovery/   # Multi-cluster analysis
│   ├── classify/                 # Message classification
│   └── input/                    # Input data files
├── hubspot_ddhq_match/           # Race matching system
│   └── google_sheets_matcher/    # Production matcher
├── ai_generated_campaign_plan/   # Campaign planning
│   ├── orchestrator.py           # Main orchestrator
│   ├── sections/                 # Plan section generators
│   └── schema/                   # Pydantic models
├── shared/                       # Shared libraries
│   ├── llm_gemini.py            # Gemini client (primary)
│   ├── braintrust.py            # LLM tracing integration
│   ├── logger.py                # Environment-aware logging
│   └── databricks_client.py     # Databricks connector
├── infrastructure/               # Terraform modules
│   ├── environments/            # dev/qa/prod configs
│   └── modules/                 # Reusable modules
└── stitch_golden_data/          # Legacy data matching (archived)
```

## Key Libraries

| Library | Purpose |
|---------|---------|
| `google-genai` | Gemini LLM completions and embeddings |
| `braintrust` | LLM call tracing and monitoring |
| `pandas`, `pyarrow` | Data processing |
| `faiss-cpu` | Vector similarity search |
| `tavily-python` | Web search integration |
| `boto3` | AWS services (S3, DynamoDB) |

## Debug Mode

Enable detailed logging:
```bash
ENVIRONMENT=development uv run <script>
```

## Deployment

Docker images are built for ARM64 (Graviton) and deployed via:
- **ECS Fargate** - Container execution
- **Step Functions** - Pipeline orchestration with retry logic
- **Lambda** - S3 trigger for automatic pipeline execution
- **Secrets Manager** - API key management

See `infrastructure/` for Terraform configurations.

## Important Notes

- **Databricks**: READ-ONLY access (SELECT queries only)
- **LLM Client**: Use `shared/llm_gemini.py` (not deprecated `llm.py`)
- **Tracing**: Braintrust integration for production LLM monitoring
- **Cost Tracking**: Built-in token usage and API cost tracking
