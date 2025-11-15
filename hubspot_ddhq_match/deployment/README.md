# DDHQ Matcher Deployment

Cloud deployment infrastructure for the HubSpot-DDHQ race matching pipeline.

## Architecture

```
Route53 (ai-{env}.goodparty.org)
  ↓
ALB (HTTPS Listener + API Key Auth)
  ↓
Lambda Function (generates run_id, starts task)
  ↓ (returns 202 Accepted in ~100ms)
ECS Fargate Task (runs 6-step pipeline, 30-60 min)
  ↓
S3 Bucket (results stored with run_id)
```

**Client Polling:** Check S3 every 20 minutes for 1 day for results.

---

## API Usage

### Endpoint

```
POST https://ai-{env}.goodparty.org/match/hubspot-ddhq
```

### Headers

```
x-api-key: <SERVE_API_KEY from AI_SECRETS_{ENV}>
Content-Type: application/json
```

### Request Body (all optional)

```json
{
  "hubspot_table": "dbt.m_general__candidacy",
  "ddhq_table": "dbt.stg_airbyte_source__ddhq_gdrive_election_results",
  "embedding_batch_size": 100,
  "embedding_max_workers": 80,
  "matching_batch_size": 1000,
  "matching_max_workers": 2000
}
```

### Response (202 Accepted)

```json
{
  "status": "STARTED",
  "run_id": "20250115_143022",
  "s3_output": {
    "bucket": "ddhq-matcher-output-dev",
    "prefix": "output/20250115_143022",
    "file": "s3://ddhq-matcher-output-dev/output/20250115_143022/matches.parquet"
  },
  "task_arn": "arn:aws:ecs:...",
  "estimated_completion": "2025-01-15T15:00:22Z",
  "config": { ... }
}
```

---

## Client Polling Example

```python
import boto3
import time
from datetime import datetime, timedelta

s3 = boto3.client('s3')

# Call API
response = requests.post(
    'https://ai-dev.goodparty.org/match/hubspot-ddhq',
    headers={'x-api-key': API_KEY},
    json={}
)

data = response.json()
run_id = data['run_id']
bucket = data['s3_output']['bucket']
parquet_key = f"{data['s3_output']['prefix']}/matches.parquet"

# Poll every 20 minutes for 2 days
start = datetime.now()
timeout = timedelta(days=2)

while datetime.now() - start < timeout:
    try:
        s3.head_object(Bucket=bucket, Key=parquet_key)
        print(f"✅ Results ready! Downloading...")
        s3.download_file(bucket, parquet_key, f'matches_{run_id}.parquet')
        break
    except s3.exceptions.NoSuchKey:
        print(f"⏳ Waiting... ({(datetime.now() - start).seconds // 60} min elapsed)")
        time.sleep(20 * 60)
else:
    print(f"❌ Timeout after 2 days")
```

---

## Local Development

### Prerequisites

- Docker
- API credentials (Databricks, Gemini)
- Environment variables in `.env` file

### Run Locally

```bash
cd hubspot_ddhq_match/deployment

# Create .env file with credentials
cat > .env << EOF
GEMINI_API_KEY=your_key
DATABRICKS_API_KEY=your_key
DATABRICKS_SERVER_HOSTNAME=your_hostname
DATABRICKS_HTTP_PATH=your_path
EOF

# Run with docker-compose
docker-compose up
```

### Test with Custom Parameters

```bash
EMBEDDING_BATCH_SIZE=150 \
MATCHING_MAX_WORKERS=1500 \
docker-compose up
```

---

## Deployment

### 1. Build and Push Docker Image

```bash
cd hubspot_ddhq_match

# Dev environment
AWS_PROFILE=work PUSH_TO_ECR=true ./build.sh dev

# QA environment
AWS_PROFILE=work PUSH_TO_ECR=true ./build.sh qa

# Prod environment
AWS_PROFILE=work PUSH_TO_ECR=true ./build.sh prod
```

Or build locally without pushing:

```bash
# Local build (auto-detects platform)
./build.sh dev

# Specify platform
PLATFORM=linux/arm64 ./build.sh dev
```

### 2. Deploy Infrastructure

```bash
# Deploy ECS + Lambda + S3
cd infrastructure/environments/dev/ddhq-matcher-fargate
terraform init
terraform apply

# Deploy ALB integration
cd infrastructure/environments/dev/shared-infra
terraform apply
```

### 3. Test API

```bash
curl -X POST https://ai-dev.goodparty.org/match/hubspot-ddhq \
  -H "x-api-key: ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

---

## Configuration

### Default Values

| Parameter | Default | Description |
|-----------|---------|-------------|
| `hubspot_table` | `dbt.m_general__candidacy` | HubSpot candidacy table |
| `ddhq_table` | `dbt.stg_airbyte_source__ddhq_gdrive_election_results` | DDHQ election results |
| `embedding_batch_size` | `100` | Batch size for embedding generation |
| `embedding_max_workers` | `80` | Concurrency for embedding generation |
| `matching_batch_size` | `1000` | Batch size for matching |
| `matching_max_workers` | `2000` | Concurrency for matching |

### Environment Variables

**Container receives:**
- `RUN_ID` - Unique run identifier (timestamp)
- `S3_OUTPUT_BUCKET` - S3 bucket for results
- `S3_OUTPUT_PREFIX` - S3 prefix (includes run_id)
- `HUBSPOT_TABLE` - HubSpot table address
- `DDHQ_TABLE` - DDHQ table address
- `EMBEDDING_BATCH_SIZE` - Step 4 batch size
- `EMBEDDING_MAX_WORKERS` - Step 4 concurrency
- `MATCHING_BATCH_SIZE` - Step 5 batch size
- `MATCHING_MAX_WORKERS` - Step 5 concurrency
- `ENVIRONMENT` - `development` or `production`
- `GEMINI_API_KEY` - From Secrets Manager
- `DATABRICKS_API_KEY` - From Secrets Manager
- `DATABRICKS_SERVER_HOSTNAME` - From Secrets Manager
- `DATABRICKS_HTTP_PATH` - From Secrets Manager

---

## Monitoring

### CloudWatch Logs

```bash
# ECS task logs
aws logs tail /ecs/ddhq-matcher-dev --follow

# Lambda logs
aws logs tail /aws/lambda/ddhq-matcher-trigger-dev --follow
```

### Check Task Status

```bash
aws ecs describe-tasks \
  --cluster ddhq-matcher-dev \
  --tasks <task-arn>
```

### Check S3 Results

```bash
aws s3 ls s3://ddhq-matcher-output-dev/output/20250115_143022/
```

---

## Cost Estimate

**Per Run (20 min):**
- Lambda: $0.0000002 (negligible)
- Fargate (4 vCPU ARM64): $0.013
- S3 storage: $0.001
- **Total: ~$0.015/run**

**Monthly (20 runs):** ~$0.30

---

## Troubleshooting

### Docker Build Fails

```bash
# Build from project root
docker build -f hubspot_ddhq_match/deployment/Dockerfile .
```

### ECR Push Fails

```bash
# Login to ECR
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin <account>.dkr.ecr.us-west-2.amazonaws.com
```

### Task Fails to Start

Check CloudWatch logs for ECS task or Lambda function.

### Pipeline Fails Midway

Check CloudWatch logs for the specific step that failed. Common issues:
- API rate limits (Gemini, Databricks)
- Out of memory (increase task memory)
- Invalid table names

---

## Pipeline Steps

1. **Data Extraction** - Pulls from Databricks (HubSpot + DDHQ tables)
2. **Data Cleaning** - Normalizes data, expands election types
3. **Temporal Filtering** - Filters by matching dates
4. **Embedding Generation** - Creates semantic embeddings (Gemini)
5. **Production Matching** - FAISS + LLM validation
6. **Runoff Enrichment** - Discovers and matches runoff elections from DDHQ

---

## Environments

### Dev
- Domain: `https://ai-dev.goodparty.org/match/hubspot-ddhq`
- S3 Bucket: `ddhq-matcher-output-dev`
- ECS Cluster: `ddhq-matcher-dev`

### QA
- Domain: `https://ai-qa.goodparty.org/match/hubspot-ddhq`
- S3 Bucket: `ddhq-matcher-output-qa`
- ECS Cluster: `ddhq-matcher-qa`

### Prod
- Domain: `https://ai.goodparty.org/match/hubspot-ddhq`
- S3 Bucket: `ddhq-matcher-output-prod`
- ECS Cluster: `ddhq-matcher-prod`
