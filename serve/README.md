# Serve - Civic Message Analysis Platform

Complete serverless platform for analyzing civic engagement messages from political campaigns through semantic clustering, classification, and AI-powered theme extraction.

## Overview

The `serve` directory contains a sophisticated ecosystem of **4 major pipelines** that transform raw campaign messages into actionable insights through data consolidation, multi-stage analysis, clustering, classification, and event publishing:

```
Raw CSV Messages → Consolidation → Clustering/Classification → Theme Extraction → Events/Reports
```

**Key Capabilities:**
- ✅ **Semantic Clustering** - Bottom-up hierarchical agglomerative clustering with optimal k selection
- ✅ **Message Classification** - Top-down hybrid LLM + rule-based classification
- ✅ **Hybrid analyze texts** - Hybrid of classify and clustering

---

## Architecture

### Pipeline Ecosystem

```
serve/
├── v1_pipeline/              # 🚀 Production-Ready Integrated Pipeline
│   ├── Orchestrates all stages with Docker + ECS Fargate deployment
│   ├── Consolidation → Clustering → Merging → Event Publishing
│   └── Output: SQS events, DynamoDB CSV, cluster analysis
│
├── hierarchical_discovery/   # 🔬 Research: Bottom-Up Clustering
│   ├── 10-stage pipeline with hierarchical agglomerative clustering
│   ├── Optimal k selection (k=5-50) via separation metrics (B/W ratio)
│   └── Output: Dendrograms, visualizations, multi-cluster analysis
│
├── classify/                 # 🔬 Research: Top-Down Classification
│   ├── 5-pass hybrid LLM + rule-based classification
│   ├── Hierarchical taxonomy with learned rules
│   └── Output: Category assignments, insights reports
│
└── analyze_texts/            # 🔬 Research: Full Multi-Stage Analysis
    ├── 6-stage pipeline: Load → Clean → Filter → Atomize → Classify → Synthesize
    ├── Hybrid clustering/AI summarization (< 20 msgs: AI, ≥ 20 msgs: cluster)
    └── Output: Atomized messages, category summaries, synthesis
```

### Research vs Production Pattern

**Research Pipelines** (standalone experimentation):
- `hierarchical_discovery/` - Clustering algorithm research
- `classify/` - Classification approach testing
- `analyze_texts/` - Full pipeline prototyping

**Production Pipeline** (`v1_pipeline/`):
- Cloud-deployable container (Docker + ECS Fargate)
- Modular design using **Adapter Pattern**
- Currently uses `hierarchical_discovery` via `ClusteringAdapter`
- Swappable: Can easily switch to `classify` or `analyze_texts` approaches

```python
# v1_pipeline uses adapters to swap research pipelines
self.clusterer = ClusteringAdapter()  # Currently: hierarchical_discovery

# Future: Swap to different approach
# self.clusterer = ClassificationAdapter()  # Use classify pipeline
# self.clusterer = AnalyzeTextsAdapter()    # Use analyze_texts pipeline
```

---

## Clustering Algorithm Evolution

We experimented with multiple clustering approaches before settling on hierarchical agglomerative clustering:

### ❌ Tried & Discarded

| Algorithm | Problem |
|-----------|---------|
| **HDBSCAN** with CVDB validation | Unpredictable results (density-based, non-deterministic), bounced around local minima jumping from 8 clusters, 40 clusters etc |
| **Leiden Algorithm** (community detection) | Unpredictable for message clustering |
| **K-means Clustering** | Unpredictable results (requires pre-specified k, sensitive to initialization) |

### ✅ Winner: Hierarchical Agglomerative Clustering

**Why it won:**
- ✅ **Predictable** - Deterministic results (no random initialization)
- ✅ **No pre-specified k** - Finds optimal k via separation metrics (B/W ratio)
- ✅ **Natural hierarchy** - Shows relationships between clusters via dendrogram
- ✅ **Complete linkage** - Robust to outliers, creates compact clusters
- ✅ **Cosine distance** - Perfect for semantic text similarity

**Configuration:**
```yaml
hierarchical:
  linkage: "complete"          # Complete linkage (deterministic)
  affinity: "cosine"           # Cosine distance for embeddings
  optimal_k_config:
    min_k: 5
    max_k: 50
```

### HDBSCAN Lives On (Different Context)

While HDBSCAN failed for main clustering, it's still used for **sub-clustering within categories** in `analyze_texts/`:

```python
# Only for categories with ≥20 messages
if len(messages) >= 20:
    import hdbscan
    clusterer = hdbscan.HDBSCAN(min_cluster_size=5, metric='euclidean')
```

**Why it works here:**
- Already inside a **classified category** (not raw messages)
- Used for finding **sub-themes** within coherent groups
- Density-based approach good for discovering natural sub-groupings
- Unpredictability acceptable since it's a secondary analysis layer

**Key Insight**: **Predictability matters most at the top level**. Once you've established stable main clusters, you can afford more exploratory methods for deeper analysis.

---

## Infrastructure & Deployment

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                   Trigger Mechanisms                         │
├─────────────────────────────────────────────────────────────┤
│  1. S3 Upload Event          2. HTTP API Trigger             │
│     └─> input/campaign.csv      └─> POST /serve/messages    │
└──────────────────┬──────────────────────────────────────────┘
                   │
                   ▼
        ┌──────────────────────┐
        │ Step Functions       │
        │ • Retry: 3 attempts  │
        │ • Backoff: 60s→120s  │
        └──────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────────┐
    │  ECS Fargate Task (ARM64)        │
    │  • 4 vCPU, 16 GB RAM             │
    │  • ~$0.20/hr (~$0.03 per run)    │
    │                                   │
    │  1. S3 sync input data            │
    │  2. Run v1_pipeline orchestrator  │
    │  3. S3 sync output results        │
    └──────────────────────────────────┘
            │           │           │
            ▼           ▼           ▼
        ┌─────┐   ┌──────┐   ┌──────────┐
        │ S3  │   │ SQS  │   │CloudWatch│
        │Output│  │FIFO  │   │  Logs    │
        └─────┘   └──────┘   └──────────┘
```

### AWS Services

| Service | Purpose | Configuration |
|---------|---------|---------------|
| **ECS Fargate** | Serverless container execution | 4 vCPU, 16 GB RAM, ARM64 (Graviton) |
| **ECR** | Docker image registry | `gp-ai-projects` repository |
| **S3** | Input/output data storage | `serve-analyze-data-${env}` |
| **SQS FIFO** | Event publishing | `develop-Queue.fifo` (dev), `master-Queue.fifo` (prod) |
| **Step Functions** | Orchestration + retry logic | 3 retries with exponential backoff |
| **Lambda** | S3 event trigger | Starts Step Functions on CSV upload |
| **CloudWatch** | Logging + monitoring | 30-day retention, Container Insights |
| **SNS** | Failure notifications | Email + Slack alerts |
| **Secrets Manager** | API keys | GEMINI_API_KEY, SERVE_API_KEY |

### Terraform Infrastructure

```
infrastructure/
├── shared/
│   ├── ecr/                    # Shared ECR repository
│   └── slack-notifier/         # Shared Slack Lambda
├── modules/
│   └── serve-analyze-fargate/  # Reusable ECS Fargate module
└── environments/
    ├── dev/serve-analyze-fargate/
    ├── qa/serve-analyze-fargate/
    └── prod/serve-analyze-fargate/
```

**Environment-Specific Configuration:**
```hcl
# Dev
docker_image_tag = "serve-analyze-dev"
sqs_queue_url = "https://sqs.us-west-2.amazonaws.com/333022194791/develop-Queue.fifo"
s3_bucket = "serve-analyze-data-dev"

# Prod
docker_image_tag = "serve-analyze-prod"
sqs_queue_url = "https://sqs.us-west-2.amazonaws.com/333022194791/master-Queue.fifo"
s3_bucket = "serve-analyze-data-prod"
```

### Cost Analysis

**ECS Fargate (ARM64 Graviton)**:
- **Hourly**: ~$0.20/hour (4 vCPU, 16 GB RAM)
- **Per Run**: ~$0.03 (typical 10-minute pipeline run)
- **Monthly**: ~$9 (300 runs/month)
- **Annual**: ~$110/year
- **Savings vs x86_64**: 20% cost reduction

**LLM Processing**:
- **Cost per 175 messages**: ~$0.05 (Gemini Flash with thinking_budget=0)
- **Cost per 1M tokens**: $0.075 (vs $2.50 without optimization)

**Total Monthly Cost (Dev)**: ~$15-20 for 300 pipeline runs

**ARM64 Optimization:**
- Dockerfile includes ARM64-specific optimizations:
  - `ENV OPENBLAS_CORETYPE=ARMV8` (BLAS optimization)
  - `ENV NUMBA_DISABLE_JIT=1` (ARM compatibility)
  - Auto-detect architecture for AWS CLI download

---

## Quick Start

### Local Development (Recommended)

```bash
# 1. Setup input data (campaign-specific subdirectory)
mkdir -p serve/input/berkley
cp /path/to/*-replies.csv serve/input/berkley/

# 2. Run pipeline
./serve/v1_pipeline/local_dev.sh berkley

# 3. Check outputs
ls -lh serve/v1_pipeline/output/consolidated/
ls -lh serve/v1_pipeline/output/consolidated/events/
```

### Docker Development

```bash
# 1. Build Docker image
./serve/v1_pipeline/build.sh

# 2. Run pipeline
CAMPAIGN_NAME=berkley docker-compose up
```

### Production Deployment

```bash
# 1. Build and push to ECR (ARM64)
AWS_PROFILE=work PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev

# 2. Trigger via S3 upload
aws s3 cp campaign.csv s3://serve-analyze-data-dev/input/campaign.csv

# 3. Or trigger via HTTP API
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "x-api-key: ${API_KEY}" \
  -d '{"campaign": "berkley", "csvS3Path": "s3://..."}'
```

---

## Pipeline Details

### V1 Pipeline (Production)

**Location**: `v1_pipeline/`

**4-Stage Pipeline**:
```
Stage 1: Data Consolidation
  ├─ Load CSV files (campaign-specific or pattern matching)
  ├─ Normalize phone numbers (+1 handling)
  ├─ Parse demographics (all optional with defaults)
  └─ Output: 300 ConsolidatedMessage objects

Stage 2: Hierarchical Discovery Clustering (112s)
  ├─ Filter STOP messages and emoji reactions (→ 188 valid)
  ├─ Split multi-part messages into atomic messages (→ 342 atomic)
  ├─ Optimal k analysis (k=5 to k=50, selects k=25)
  ├─ Theme analysis with Gemini LLM
  └─ Output: 342 clustering results with multi_cluster_data

Stage 3: Data Merging (0.1s)
  ├─ Match clustering by atomic_id (342 matches)
  ├─ Create UnifiedCampaignRecord objects
  ├─ Export comprehensive cluster analysis CSV
  └─ Export DynamoDB preview CSV with quotes

Stage 4: Event Publishing (0.1s)
  ├─ Rank clusters by unique respondent count
  ├─ Select top 3 clusters (configurable)
  ├─ Generate pollIssueAnalysis events with quotes
  ├─ Generate pollAnalysisComplete event
  ├─ Save to S3: events/*.json
  └─ (Optional) Publish to SQS FIFO queue

Total: ~120s for 300 messages → 342 atomic messages → 342 output records
```

**Output Files**:
```
serve/v1_pipeline/output/
├── consolidated/
│   ├── berkley_all_cluster_analysis.csv      # All atomic messages with multi-cluster data
│   ├── discovery_reports/
│   │   ├── separation_first_optimal_k_*.md   # Optimal k selection report
│   │   └── k_comparison_table_*.csv          # K-value comparison
│   ├── dynamodb_preview/
│   │   └── berkley_dynamodb_records_*.csv    # Includes quotes with phone attribution
│   └── events/
│       └── events_*.json                      # SQS/API event payloads (primary output)
└── logs/
    └── pipeline.log
```

**Event Format**:
```json
[
  {
    "type": "pollIssueAnalysis",
    "data": {
      "pollId": "berkley",
      "rank": 1,
      "theme": "Roads & Street Maintenance",
      "summary": "Citizens express frustration with poor road conditions...",
      "analysis": "Detailed analysis of underlying issues...",
      "quotes": [
        {"quote": "Side streets are in horrible shape", "phone_number": "2485619334"},
        {"quote": "My street needs full pavement", "phone_number": "2487211260"}
      ],
      "responseCount": 32
    }
  },
  {
    "type": "pollAnalysisComplete",
    "data": {
      "pollId": "berkley",
      "totalResponses": 149
    }
  }
]
```

### Hierarchical Discovery (Research)

**Location**: `hierarchical_discovery/`

**10-Stage Pipeline**:
```
Stage 1: Data Loader          → Load CSV with metadata preservation
Stage 2: Content Filter        → Remove STOP/unsubscribe, emoji reactions
Stage 3: AI Message Processor  → LLM-powered cleaning and splitting
Stage 4: Embedding Generator   → 3072d semantic embeddings (Gemini)
Stage 5: Dimensionality Reducer → PCA (50d) + UMAP (15d)
Stage 6: Hierarchical Clustering → Complete linkage, cosine distance
Stage 7: Optimal K Selection   → Separation-first scoring (B/W ratio)
Stage 8: Multi-Cluster Analyzer → Parallel LLM theme analysis (50 workers)
Stage 9: Cluster Merger        → Two-stage: embedding similarity + LLM validation
Stage 10: Visualization        → Dendrograms, charts, reports
```

**Optimal K Selection Philosophy**:
- **Separation (50% weight)**: B/W ratio ≥ 50 = exceptional
- **Cohesion (30% weight)**: Silhouette score ≥ 0.6 = good
- **Balance (20% weight)**: Low CV = more balanced
- **Key Insight**: Cluster size doesn't matter. Separation does.

**Performance**:
- 175 messages in ~2 minutes
- Cost: ~$0.05 per run
- Scalability: 1000 messages in ~10 minutes

### Classify Pipeline (Research)

**Location**: `classify/`

**5-Pass Classification**:
```
Pass 1: Rule-based uncategorization (federal politics, wrong numbers)
Pass 2: LLM issue identification with hierarchical taxonomy
Pass 3: Apply learned classification rules (property taxes, trucks)
Pass 4: Context refinement (root cause detection)
Pass 5: Overall sentiment and quality assessment
```

**Hierarchical Taxonomy**:
- `infrastructure_and_transportation` - Roads, transit, traffic
- `public_safety` - Police, fire, emergency
- `education` - Schools, programs, safety
- `housing_and_development` - Zoning, housing, taxes
- `health_and_human_services` - Health, mental health, seniors
- `economic_development` - Jobs, business, industry
- `quality_of_life` - Parks, utilities, environment
- `government_operations` - Budget, transparency, engagement

**Performance**:
- 1200 concurrent LLM connections
- ~10,000 messages/minute throughput
- Cost: thinking_budget=0 ($0.075/1M tokens)

### Analyze Texts (Research)

**Location**: `analyze_texts/`

**6-Stage Pipeline**:
```
Stage 0: Data Loading           → Load CSV with demographics
Stage 1: Data Cleaning          → Normalize whitespace, fix encoding
Stage 2: Data Filtering         → Remove STOP/unsubscribe messages
Stage 3: Atomization            → Split compound messages, anonymize
Stage 4: Classification         → Multi-category classification
Stage 5: Synthesis              → Smart summarization (AI or clustering)
Stage 6: Hierarchical Re-analysis → Optional additional layer
```

**Smart Synthesis**:
- **Small categories (< 20 messages)**: Direct AI summarization
- **Large categories (≥ 20 messages)**:
  1. Generate embeddings
  2. Cluster with HDBSCAN
  3. Parallel cluster analysis
  4. Aggregate cluster summaries

---

## Data Flow

```
📁 Raw CSV Files
     ↓
📊 Consolidation
     ├─ Phone normalization (+1 handling, digits only)
     ├─ Demographics parsing (all optional)
     └─ Poll ID extraction (filename or column)
     ↓
🧬 Hierarchical Discovery Clustering
     ├─ STOP/emoji filtering
     ├─ Message splitting (compound → atomic)
     ├─ Embedding generation (3072d Gemini)
     ├─ Dimensionality reduction (PCA 50d + UMAP 15d)
     ├─ Hierarchical clustering (complete linkage, cosine)
     ├─ Optimal k selection (k=5-50, B/W ratio scoring)
     ├─ Theme analysis (parallel LLM, 50 workers)
     └─ Cluster merging (embedding + LLM validation)
     ↓
🔗 Data Merging
     ├─ Match by atomic_id (not phone_number)
     ├─ Preserve metadata through transformations
     └─ Create UnifiedCampaignRecord objects
     ↓
📤 Event Publishing
     ├─ Rank clusters (unique respondent count)
     ├─ Select top 3 clusters
     ├─ Build pollIssueAnalysis events with quotes
     ├─ Build pollAnalysisComplete event
     ├─ Save to S3 (always)
     └─ Publish to SQS (optional)
```

---

## Input Data Format

### Minimum Required Fields

```csv
phone_number,message_text
2485619334,Side streets are in horrible shape
2487211260,My street needs full pavement
```

### Full Format (All Optional Fields)

```csv
"Campaign ID","Campaign Name","Contact Phone Number","Carrier","Sent At","Message Text","round","poll_id","voters_age","age_group","location","ward","voters_gender","voting_performance_category","residence_addresses_city","homeowner_status","business_owner","has_children_under_18","education_level","income_level"
```

**Field Mappings** (backward compatible):
- `phone_number` or `Contact Phone Number`
- `message_text` or `Message Text`
- `sent_at` or `Sent At`
- All demographic fields default to "Unknown" if not provided

---

## Performance Characteristics

### Throughput

| Pipeline | Configuration | Throughput | Notes |
|----------|---------------|-----------|--------|
| **Hierarchical Discovery** | 175 msgs | ~2 min total | Includes clustering, analysis |
| **Classify** | 1200 workers | ~10,000 msgs/min | Peak throughput |
| **Analyze Texts** | 1200 workers | ~10,000 msgs/min | Peak throughput |
| **V1 Pipeline** | 300 msgs | ~120 sec total | End-to-end with clustering |

### Cost Estimates (Gemini Flash)

| Stage | Typical Input | Cost |
|-------|---------------|------|
| Message Processing | 50K tokens | $0.013 |
| Embeddings | 25K tokens | $0.002 |
| Theme Analysis | 200K tokens | $0.030 |
| Cluster Merger | 30K tokens | $0.004 |
| **Total (175 msgs)** | **305K tokens** | **~$0.049** |

### Scalability

- **Small**: 100 messages → 1.5 min, $0.03
- **Medium**: 200 messages → 2.5 min, $0.06
- **Large**: 500 messages → 5 min, $0.15
- **Very Large**: 1000 messages → 10 min, $0.30

---

## High-Throughput Parallelization Pattern

For maximum performance, use the proven parallel pattern with ThreadPoolExecutor + asyncio.gather():

```python
from concurrent.futures import ThreadPoolExecutor
import asyncio
from shared.llm_gemini import GeminiClient, GeminiModelType

class HighThroughputProcessor:
    def __init__(self, target_concurrency: int = 1200):
        # ThreadPoolExecutor for maximum concurrency
        self.max_workers = target_concurrency
        self.thread_pool = ThreadPoolExecutor(max_workers=self.max_workers)

        # High-concurrency LLM client configuration
        self.llm_client = GeminiClient(
            default_model=GeminiModelType.FLASH,
            default_temperature=0.0,
            thinking_budget=0,  # Cost efficiency (~$0.075/1M tokens)
            max_connections=target_concurrency,
            max_keepalive_connections=target_concurrency // 4
        )

    async def process_item_async(self, item, index):
        """Process single item with non-blocking LLM call"""
        response = await asyncio.get_event_loop().run_in_executor(
            self.thread_pool,
            lambda: self.llm_client.generate_content(
                prompt=f"Process item: {item}",
                max_tokens=1000
            )
        )
        return response

    async def process_all_items(self, items):
        """Process ALL items as individual concurrent tasks"""
        all_tasks = [self.process_item_async(item, idx) for idx, item in enumerate(items)]
        results = await asyncio.gather(*all_tasks, return_exceptions=True)
        return results
```

**Key Performance Patterns**:
- ✅ **ThreadPoolExecutor**: Non-blocking LLM calls (prevents async event loop blocking)
- ✅ **Individual Tasks**: One asyncio task per item, not batch-level parallelism
- ✅ **High Concurrency**: 1200+ concurrent connections (10k+ items/minute)
- ✅ **asyncio.gather()**: Execute all tasks simultaneously
- ✅ **Cost Efficiency**: `thinking_budget=0` reduces cost from $2.50 to $0.075 per million tokens
- ✅ **Performance**: 23x+ speed improvements (e.g., 0.6 → 13.8 items/sec)

---

## Configuration

### Environment Variables

```bash
# Required
GEMINI_API_KEY=your_key_here

# Optional
ENVIRONMENT=development              # Enable debug logging
S3_OUTPUT_BUCKET=bucket-name         # S3 bucket for event files
SQS_QUEUE_URL=queue-url              # SQS FIFO queue URL (optional)
```

### Pipeline Config (`v1_pipeline/config/pipeline_config.yaml`)

```yaml
pipeline:
  mode: "integrated"                 # Run all stages

clustering:
  enabled: true
  min_messages_for_clustering: 10    # Minimum for meaningful results

sqs_events:
  enabled: true
  publish_to_sqs: false              # Set true to enable SQS publishing
  publish_top_n: 3                   # Number of top clusters to publish
  min_unique_respondents: 1          # Minimum respondents per cluster
```

---

## Monitoring & Troubleshooting

### Check Pipeline Status

```bash
# View local logs
tail -f serve/v1_pipeline/logs/pipeline.log

# View ECS logs
aws logs tail /ecs/serve-analyze-dev --follow

# List running tasks
aws ecs list-tasks --cluster serve-analyze-dev

# Check Step Functions execution
aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-dev
```

### Common Issues

**No CSV files found:**
- Check `serve/input/<campaign>/` exists and contains .csv files
- Or ensure CSV filenames contain campaign name for pattern matching

**Pipeline timeout:**
- Reduce dataset size for testing
- Check Gemini API key is valid
- Increase timeout in config

**Output files missing:**
- Verify `serve/v1_pipeline/output/` directory exists
- Check permissions on output directory
- Review logs for write errors

**ECS task fails immediately:**
- Check CloudWatch logs: `/ecs/serve-analyze-dev`
- Verify Secrets Manager permissions
- Ensure ECR image exists with correct tag

---

## Project Structure

```
serve/
├── README.md                        # ⭐ This file
├── v1_pipeline/                     # Production-ready integrated pipeline
│   ├── adapters/
│   │   └── clustering_adapter.py    # Bridge to hierarchical_discovery
│   ├── config/
│   │   └── pipeline_config.yaml     # Pipeline configuration
│   ├── models/
│   │   ├── events.py                # SQS event models
│   │   └── unified_record.py        # Data models
│   ├── pipeline/
│   │   ├── orchestrator.py          # Main orchestration (900 lines)
│   │   └── sqs_publisher.py         # Event publishing
│   ├── scripts/
│   │   └── run_pipeline.py          # CLI entry point
│   ├── stages/
│   │   └── llm_cluster_recommender.py # LLM-based cluster ranking
│   ├── Dockerfile                   # Multi-arch Docker build
│   ├── docker-compose.yml           # Local Docker Compose
│   ├── entrypoint.sh                # Container entrypoint
│   ├── local_dev.sh                 # ⭐ Local development runner
│   ├── build.sh                     # Docker build + ECR push
│   ├── BUILD_NOTES.md               # ARM64 optimization notes
│   └── README.md                    # V1 pipeline documentation
│
├── hierarchical_discovery/          # Research: Bottom-up clustering
│   ├── config.yaml                  # Clustering configuration
│   ├── orchestrator.py              # Main coordinator (428 lines)
│   ├── models.py                    # Data models
│   ├── find_optimal_k.py            # Optimal K algorithm
│   ├── run_pipeline.py              # CLI entry point
│   ├── OPTIMAL_K_APPROACH.md        # Philosophy and approach
│   ├── utils/                       # 10 utility modules
│   ├── stages/                      # 10 processing stages
│   └── README.md                    # Hierarchical discovery docs
│
├── classify/                        # Research: Top-down classification
│   ├── config.yaml                  # Classification configuration
│   ├── run_pipeline.py              # Main orchestrator
│   ├── models.py                    # Pydantic data models
│   ├── smart_classifier.py          # Multi-pass LLM classifier
│   ├── classification_rules.py      # Rule-based logic
│   ├── batch_processor.py           # High-throughput processor
│   └── README.md                    # Classification docs
│
├── analyze_texts/                   # Research: Full multi-stage analysis
│   ├── config.yaml                  # Pipeline configuration
│   ├── orchestrator.py              # Main coordinator
│   ├── models.py                    # Data models
│   ├── scripts/
│   │   └── run_pipeline.py          # CLI entry point
│   ├── stages/                      # 6 processing stages
│   └── README.md                    # Analyze texts docs
│
├── data/                            # Test CSV data files
├── input/                           # Input data (campaign-specific)
├── output/                          # Analysis reports and results
├── results/                         # Processed results with embeddings
├── cache/                           # Caching directory
└── logs/                            # Debug and error logs
```

---

## Related Documentation

### V1 Pipeline
- [v1_pipeline/README.md](./v1_pipeline/README.md) - Production pipeline documentation
- [v1_pipeline/BUILD_NOTES.md](./v1_pipeline/BUILD_NOTES.md) - ARM64/Graviton optimization

### Research Pipelines
- [hierarchical_discovery/README.md](./hierarchical_discovery/README.md) - Clustering pipeline details
- [hierarchical_discovery/OPTIMAL_K_APPROACH.md](./hierarchical_discovery/OPTIMAL_K_APPROACH.md) - Optimal K philosophy
- [classify/README.md](./classify/README.md) - Classification pipeline details
- [analyze_texts/README.md](./analyze_texts/README.md) - Multi-stage analysis details

### Infrastructure
- [../infrastructure/modules/serve-analyze-fargate/](../infrastructure/modules/serve-analyze-fargate/) - Terraform module
- [../infrastructure/environments/dev/serve-analyze-fargate/](../infrastructure/environments/dev/serve-analyze-fargate/) - Dev environment

---

## Development Workflow

### 1. Local Development (Research)

```bash
# Test hierarchical discovery
cd serve/hierarchical_discovery
uv run run_pipeline.py --data-source berkley

# Test classification
cd serve/classify
uv run run_pipeline.py --data-source berkley

# Test full analysis
cd serve/analyze_texts
uv run scripts/run_pipeline.py --campaign berkley
```

### 2. Test V1 Pipeline Locally

```bash
# Use local_dev.sh for quick testing
./serve/v1_pipeline/local_dev.sh berkley

# Or run directly
uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley
```

### 3. Build & Deploy to AWS

```bash
# Build Docker image
./serve/v1_pipeline/build.sh

# Build and push to ECR (ARM64)
AWS_PROFILE=work PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev

# Trigger via S3 upload
aws s3 cp campaign.csv s3://serve-analyze-data-dev/input/campaign.csv

# Monitor execution
aws logs tail /ecs/serve-analyze-dev --follow
```

### 4. Deploy Infrastructure Changes

```bash
# Update Terraform
cd infrastructure/environments/dev/serve-analyze-fargate
terraform plan
terraform apply

# Update task definition
terraform apply -target=aws_ecs_task_definition.pipeline
```

---

## Key Features

### 100% Phone Attribution

Fuzzy string matching ensures accurate quote attribution through all transformations:

```python
# Example quote with phone attribution
{
  "quote": "Side streets are in horrible shape",
  "phone_number": "2485619334"
}
```

Metadata is preserved through all stages:
```
RawMessage → FilteredMessage → AtomicMessage → EmbeddedMessage → ClusteredMessage
```

### Multi-Cluster Analysis

One atomic message can have clustering data for **all k values** (k=5 through k=50):

```csv
atomic_id,phone_number,k5_cluster_id,k5_theme,k10_cluster_id,k10_theme,k25_cluster_id,k25_theme,...
uuid-123,2485619334,2,Infrastructure,5,Roads,12,Street Maintenance,...
```

### Message Splitting (Atomization)

Compound messages are automatically split into atomic messages:

**Input**: "Fix roads. Also schools are falling apart."
**Output**:
1. "Fix roads in the local area."
2. "Schools in the local area are falling apart."

Both messages retain the same phone number for attribution.

---

## Architecture Principles

1. **Modularity** - Each pipeline/stage is independent and composable
2. **Async-First** - Uses asyncio for non-blocking operations
3. **Cost Efficiency** - thinking_budget=0 on Gemini Flash ($0.075/1M vs $2.50/1M)
4. **Metadata Preservation** - Full lineage tracking through all transformations
5. **100% Phone Attribution** - Fuzzy string matching ensures accurate quote attribution
6. **Parallel Processing** - ThreadPoolExecutor + asyncio.gather() for 10k+ msgs/min
7. **Configuration-Driven** - YAML configs for algorithm parameters and paths
8. **Error Resilience** - Checkpoints, logging, graceful degradation
9. **ARM64 Optimization** - Graviton-optimized Docker for 20% cost savings
10. **Production-Ready** - Comprehensive logging, metrics, event publishing

---

## Summary

The `serve` directory implements a **production-ready, serverless, cost-optimized** civic message analysis platform with:

✅ **Event-driven** - S3 upload or HTTP API triggers pipeline
✅ **Resilient** - Step Functions with 3 retries and exponential backoff
✅ **Monitored** - CloudWatch logs, Container Insights, SNS + Slack alerts
✅ **Cost-effective** - ARM64 Graviton saves 20%, ~$15-20/month for 300 runs (we could probably switch down to spot to save more, since these are not time sensitive and if they are )
✅ **Secure** - Private subnets, Secrets Manager, encrypted S3, IAM least privilege
✅ **Scalable** - Fargate auto-scales, no server management
✅ **Infrastructure as Code** - Terraform for all environments (dev/qa/prod)
✅ **Research-to-Production** - Modular adapter pattern for swapping algorithms

The architecture supports both **research** (local development) and **production** (ECS Fargate) workflows with the same Docker container, ensuring consistency across environments.

---

## Contact & Support

For questions or issues:
- Check individual pipeline README files for detailed documentation
- Review CloudWatch logs for debugging
- Consult Terraform modules for infrastructure questions
- See CLAUDE.md files for development guidelines
