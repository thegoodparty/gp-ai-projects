# V1 Pipeline - Message Analysis & Clustering

Complete pipeline for processing campaign messages through consolidation, hierarchical clustering, and event publishing.

## Quick Start

### Local Development (Recommended)

```bash
# 1. Setup input data directory (campaign-specific subdirectory)
mkdir -p serve/input/berkley
cp /path/to/*-replies.csv serve/input/berkley/

# 2. Run pipeline
./serve/v1_pipeline/local_dev.sh berkley

# 3. Check outputs
ls -lh serve/v1_pipeline/output/consolidated/
ls -lh serve/v1_pipeline/output/consolidated/events/
```

The local_dev.sh script:
- Auto-detects campaign CSV files in `serve/input/<campaign>/` or by pattern matching
- Uses absolute paths for reliable output placement
- Creates temporary config with optimal settings
- Processes all rounds and generates comprehensive reports

### Docker Deployment

```bash
# 1. Setup data directory
mkdir -p serve/v1_pipeline/data
cp /path/to/campaign.csv serve/v1_pipeline/data/

# 2. Build and run
./serve/v1_pipeline/build.sh
CAMPAIGN_NAME=berkley docker-compose up
```

### AWS ECS Fargate (Production)

See [DEPLOYED_USAGE.md](./DEPLOYED_USAGE.md) for complete deployment guide.

```bash
# Trigger via HTTP API
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{
    "campaign": "berkley",
    "csvS3Path": "s3://serve-analyze-data-dev/input/berkley.csv"
  }'
```

## Input Data Format

### Directory Structure

**Local Development:**
```
serve/input/
├── berkley/                    # Campaign-specific subdirectory (recommended)
│   ├── R1-replies.csv
│   ├── R2-replies.csv
│   └── R3-replies.csv
└── cara/
    └── consolidated.csv
```

**Docker:**
```
serve/v1_pipeline/data/
└── campaign.csv
```

### CSV Format Requirements

**Minimum Required Fields:**
```csv
phone_number,message_text
2485619334,Side streets are in horrible shape
2487211260,My street needs full pavement
```

**Supported Fields (backward compatible):**

| Field Name | Old Format | Required | Description |
|------------|-----------|----------|-------------|
| `phone_number` | `Contact Phone Number` | ✅ Yes | Phone number (auto-normalized) |
| `message_text` | `Message Text` | ✅ Yes | Message content |
| `sent_at` | `Sent At` | No | ISO timestamp (defaults to now) |
| `round` | - | No | Round identifier (R1, R2, R3) |
| `poll_id` | - | No | Poll identifier (defaults to filename) |
| `campaign_id` | `Campaign ID` | No | Campaign UUID |
| `campaign_name` | `Campaign Name` | No | Campaign display name |
| `carrier` | `Carrier` | No | Mobile carrier |

**Optional Demographics:**
- `voters_age`, `age_group`, `location`, `ward`
- `voters_gender`, `voting_performance_category`
- `residence_addresses_city` (→ `residence_city`)
- `homeowner_status`, `business_owner`
- `has_children_under_18`, `education_level`, `income_level`

All demographic fields default to "Unknown" if not provided.

### Example CSV

```csv
"Campaign ID","Campaign Name","Contact Phone Number","Carrier","Sent At","Message Text","round"
"0198cd60-856c","City of Berkley MI","+12484709513","VERIZON","2025-08-27T16:00:47.000Z","Side streets are in horrible shape","R1"
```

## Output Files

### Directory Structure

```
serve/v1_pipeline/
├── output/
│   └── consolidated/
│       ├── berkley_all_cluster_analysis.csv         # All atomic messages with multi-cluster data
│       ├── discovery_reports/                        # Hierarchical discovery analysis
│       │   ├── separation_first_optimal_k_*.md      # Optimal k selection report
│       │   └── k_comparison_table_*.csv             # K-value comparison table
│       ├── dynamodb_preview/                         # CSV exports with quotes
│       │   └── berkley_dynamodb_records_*.csv       # ⭐ Includes quotes field
│       └── events/                                   # 🎯 Primary output for API
│           └── events_*.json                         # SQS/API event payloads
└── logs/
    └── pipeline.log
```

### Output Formats

**1. Events JSON (Primary Output)**

Ready for SQS publishing or API consumption:

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
        {
          "quote": "Side streets are in horrible shape",
          "phone_number": "2485619334"
        },
        {
          "quote": "My street needs full pavement",
          "phone_number": "2487211260"
        }
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

**2. DynamoDB Records CSV**

Preview/analysis CSV with all fields including quotes:

```csv
"campaign_id","poll_id","record_id","phone_number","message","atomic_message","theme","summary","analysis","quotes","category","sentiment",...
"berkley","berkley-R1","discover#2485619334#uuid","2485619334","Side streets...","Side streets in the local area are in horrible shape.","Roads & Street Maintenance","Citizens express...","Detailed analysis...","Side streets... [2485619334]; My street... [2487211260]","Infrastructure","frustrated"
```

**Recent Improvement:** ✨ Quotes field now included with phone attribution

**3. Comprehensive Cluster Analysis CSV**

All atomic messages with multi-cluster assignments (k=5 through k=50):
- Used for analysis and comparison
- Shows cluster assignments across different k values
- Includes original and atomic message variants

## Data Flow

```
📁 Input CSV Files (serve/input/<campaign>/*.csv)
     ↓
📊 Stage 1: Data Consolidation
     ├─ Load CSV files (campaign-specific or pattern matching)
     ├─ Normalize phone numbers (+1 handling, non-digits removed)
     ├─ Parse demographics (all optional with defaults)
     ├─ Extract poll_id from filename or column
     └─ Output: 300 ConsolidatedMessage objects
     ↓
🧬 Stage 2: Hierarchical Discovery Clustering (112s)
     ├─ Filter STOP messages and emoji reactions (→ 188 valid)
     ├─ Split multi-part messages into atomic messages (→ 342 atomic)
     ├─ Optimal k analysis (k=5 to k=50, selects k=25)
     ├─ Theme analysis with Gemini LLM
     ├─ Generate verbatim quotes with phone attribution
     ├─ Preserve discovery reports and plots
     └─ Output: 342 clustering results with multi_cluster_data
     ↓
🔗 Stage 3: Data Merging (0.1s)
     ├─ Match clustering by atomic_id (342 matches)
     ├─ Create UnifiedCampaignRecord objects
     ├─ Export comprehensive cluster analysis CSV
     └─ Export dynamodb_preview CSV with quotes
     ↓
🏆 Stage 3.5: LLM Cluster Recommendations (4s, optional)
     ├─ Aggregate cluster statistics
     ├─ LLM selects top 3 clusters with reasoning
     └─ Generate overall assessment
     ↓
📤 Stage 4: Event Publishing (0.1s)
     ├─ Rank clusters by unique respondent count
     ├─ Select top 3 clusters (configurable)
     ├─ Generate pollIssueAnalysis events with quotes
     ├─ Generate pollAnalysisComplete event
     ├─ Save to local: events/*.json
     └─ (Optional) Publish to SQS FIFO queue

Total: ~120s for 300 messages → 342 atomic messages → 342 output records
```

### Pipeline Metrics

**Example Output:**
```
Campaign Id: berkley
Input Messages: 300        # Original CSV rows
Atomic Messages: 342       # After message splitting
Messages Expanded: +42     # Multi-part messages split
Output Records: 342        # Final unified records
Success Rate: 100.0%       # output_records / atomic_messages
Processing Time: 120.96s
```

**Key Changes in v1.1:**
- ✨ Metrics now show message expansion from splitting
- ✨ Success rate calculated against atomic messages (not input)
- ✨ Clear tracking of data flow through pipeline

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

### Pipeline Config (`config/pipeline_config.yaml`)

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

## Docker Build & Deployment

### Local Build

```bash
# Build locally (auto-detects architecture)
./serve/v1_pipeline/build.sh

# Force specific platform
PLATFORM=linux/arm64 ./serve/v1_pipeline/build.sh
```

### ECR Deployment

```bash
# Build and push to ECR (ARM64 for Graviton/cost savings)
AWS_PROFILE=work PUSH_TO_ECR=true ./serve/v1_pipeline/build.sh dev

# Tag retention rules
# - dev/prod/main: Never expire
# - v1.0.0: 365 days
# - latest: 180 days
```

**ECR Repository:**
- Name: `gp-ai-projects`
- URL: `333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects`
- Region: us-west-2

See [BUILD_NOTES.md](./BUILD_NOTES.md) for ARM64/Graviton optimization details.

## Project Structure

```
serve/v1_pipeline/
├── adapters/
│   └── clustering_adapter.py        # Hierarchical discovery integration
├── config/
│   └── pipeline_config.yaml         # Pipeline configuration
├── models/
│   ├── events.py                    # SQS event models
│   └── unified_record.py            # Data models and PipelineResult
├── pipeline/
│   ├── orchestrator.py              # Main pipeline orchestration
│   └── sqs_publisher.py             # Event publishing logic
├── scripts/
│   ├── run_pipeline.py              # CLI entry point
│   └── trigger_via_http.sh          # HTTP API examples
├── stages/
│   └── llm_cluster_recommender.py   # LLM-based cluster ranking
├── Dockerfile                       # Multi-arch Docker build
├── docker-compose.yml               # Local Docker Compose
├── entrypoint.sh                    # Container entrypoint with S3 sync
├── local_dev.sh                     # ⭐ Local development runner
├── build.sh                         # Docker build script
└── run.sh                           # Docker run wrapper
```

## Usage Examples

### Local Development

```bash
# Basic run
./serve/v1_pipeline/local_dev.sh berkley

# With debug logging
ENVIRONMENT=development ./serve/v1_pipeline/local_dev.sh berkley

# Check outputs
cat serve/v1_pipeline/output/consolidated/events/events_*.json
head serve/v1_pipeline/output/consolidated/dynamodb_preview/berkley_*.csv
```

### Docker Compose

```bash
# Standard run
CAMPAIGN_NAME=berkley docker-compose up

# Debug mode
CAMPAIGN_NAME=berkley DEBUG=true docker-compose up

# Skip clustering (faster)
CAMPAIGN_NAME=berkley SKIP_CLUSTERING=true docker-compose up
```

### Python Script

```bash
# From project root
uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley

# With options
uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign berkley \
  --skip-clustering \
  --save-results results.json
```

## Monitoring & Troubleshooting

### Check Pipeline Status

```bash
# View logs
tail -f serve/v1_pipeline/logs/pipeline.log

# Check metrics
grep "PIPELINE RESULTS" serve/v1_pipeline/logs/pipeline.log
```

### Common Issues

**No CSV files found:**
- Check `serve/input/<campaign>/` exists and contains .csv files
- Or ensure CSV filenames contain campaign name for pattern matching

**Pipeline timeout:**
- Reduce dataset size for testing
- Check Gemini API key is valid
- Increase timeout in config (default: 2 minutes per stage)

**Output files missing:**
- Verify `serve/v1_pipeline/output/` directory exists
- Check permissions on output directory
- Review logs for write errors

## Related Documentation

- [DEPLOYED_USAGE.md](./DEPLOYED_USAGE.md) - AWS ECS Fargate deployment guide
- [BUILD_NOTES.md](./BUILD_NOTES.md) - ARM64/Graviton build optimization
- [../hierarchical_discovery/README.md](../hierarchical_discovery/README.md) - Clustering pipeline details

## Recent Updates

### v1.1 (October 2025)
- ✨ Added quotes field to DynamoDB preview CSV with phone attribution
- ✨ Improved CSV quoting (QUOTE_ALL) for proper parsing
- ✨ Enhanced metrics reporting with message expansion tracking
- ✨ Fixed output path resolution for local development
- 📊 Clarified success rate calculation (output/atomic vs output/input)

### v1.0 (September 2025)
- 🎯 Complete pipeline integration with hierarchical discovery
- 📤 Event publishing to S3 (SQS coming soon)
- 🏆 LLM-based cluster recommendations
- 🐳 Docker deployment with ARM64 support
- ☁️ AWS ECS Fargate production deployment
