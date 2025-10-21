# V1 Pipeline Fargate - Usage Examples

## Complete End-to-End Flow

### 1. Someone Makes a Request

A user or service sends a POST request to trigger the pipeline:

```bash
curl -X POST https://api.example.com/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $API_TOKEN" \
  -d '{
    "campaign": "berkley",
    "csvData": "round,phone,message,sentiment\n1,+1234567890,Hello world,positive\n1,+1987654321,Thanks for calling,positive\n",
    "testMode": false
  }'
```

### 2. Lambda Receives Request and Validates

The `pipeline-trigger` Lambda function:
1. Validates the request (checks for required `campaign` field)
2. Uploads the CSV data to S3 at `s3://serve-analyze-data-dev/input/berkley/{timestamp}.csv`
3. Prepares environment variables for the ECS task

### 3. Lambda Triggers Fargate Task

Lambda calls the ECS `RunTask` API:

```typescript
const runTaskCommand = new RunTaskCommand({
  cluster: 'serve-analyze-dev',
  taskDefinition: 'serve-analyze-dev:5',
  launchType: 'FARGATE',
  networkConfiguration: {
    awsvpcConfiguration: {
      subnets: ['subnet-xxx', 'subnet-yyy'],
      securityGroups: ['sg-xxx'],
      assignPublicIp: 'ENABLED'
    }
  },
  overrides: {
    containerOverrides: [{
      name: 'serve-analyze',
      environment: [
        { name: 'CAMPAIGN_NAME', value: 'berkley' },
        { name: 'S3_INPUT_PATH', value: 's3://serve-analyze-data-dev/input/berkley/1234567890.csv' },
        { name: 'S3_OUTPUT_PATH', value: 's3://serve-analyze-data-dev/output/berkley/1234567890/' },
        { name: 'API_URL', value: 'https://ai-dev.goodparty.org' },
        { name: 'ENVIRONMENT', value: 'production' },
        { name: 'TEST_MODE', value: 'false' }
      ]
    }]
  }
});
```

### 4. Lambda Returns Task ARN

```json
{
  "taskArn": "arn:aws:ecs:us-east-1:123456789:task/serve-analyze-dev/abc123def456",
  "campaign": "berkley",
  "inputS3Path": "s3://serve-analyze-data-dev/input/berkley/1234567890.csv",
  "outputS3Path": "s3://serve-analyze-data-dev/output/berkley/1234567890/",
  "message": "Pipeline task started successfully"
}
```

### 5. Fargate Spins Up Container

AWS Fargate:
1. Pulls the Docker image from ECR: `serve-analyze:latest`
2. Allocates 4 vCPUs and 16GB RAM
3. Injects secrets from AWS Secrets Manager:
   - `GEMINI_API_KEY`
   - `API_KEY`
4. Starts the container with the environment variables from step 3

**Container starts in ~30-60 seconds**

### 6. Container Runs Pipeline

The `entrypoint.sh` script executes:

```bash
# Download data from S3
aws s3 cp s3://serve-analyze-data-dev/input/berkley/1234567890.csv /app/data/ --quiet

# Run pipeline
python serve/serve_analyze/scripts/run_pipeline.py \
  --campaign berkley
```

Pipeline stages:
1. **Data Consolidation**: Load CSV → group by respondent → consolidate messages
2. **Classification**: Classify message sentiment, topics, and categories
3. **Clustering**: Multi-cluster analysis (10, 15, 20, 25 clusters)
4. **Upload**: Write results to DynamoDB via API

### 7. Container Uploads Results

After successful pipeline completion:

```bash
# Upload generated files to S3
aws s3 sync /app/output/ s3://serve-analyze-data-dev/output/berkley/1234567890/ --quiet

# Files uploaded:
# - berkley_consolidated_respondents.csv
# - berkley_classified_messages.csv
# - berkley_clustered_data_10.csv
# - berkley_clustered_data_15.csv
# - berkley_clustered_data_20.csv
# - berkley_clustered_data_25.csv
```

### 8. Container Exits

- Exit code 0 = success
- Fargate automatically terminates the task
- CloudWatch logs retained for 30 days
- **No ongoing costs** - infrastructure scales to zero

### 9. Results Available

**DynamoDB Records:**
```bash
aws dynamodb query \
  --table-name serve-message-v1-dev \
  --key-condition-expression "PK = :pk" \
  --expression-attribute-values '{":pk": {"S": "CAMPAIGN#berkley"}}'
```

**S3 Output Files:**
```bash
aws s3 ls s3://serve-analyze-data-dev/output/berkley/1234567890/

# Download results
aws s3 cp s3://serve-analyze-data-dev/output/berkley/1234567890/ ./results/ --recursive
```

## Monitoring Task Progress

### Option 1: Poll Task Status

```bash
TASK_ARN="arn:aws:ecs:us-east-1:123456789:task/serve-analyze-dev/abc123def456"

aws ecs describe-tasks \
  --cluster serve-analyze-dev \
  --tasks $TASK_ARN \
  --query 'tasks[0].{Status:lastStatus,StartedAt:startedAt,StoppedAt:stoppedAt,StopCode:stopCode}' \
  --output table
```

**Task States:**
- `PROVISIONING`: Fargate allocating resources
- `PENDING`: Container starting
- `RUNNING`: Pipeline executing
- `DEPROVISIONING`: Task shutting down
- `STOPPED`: Task completed (check `stopCode` for success/failure)

### Option 2: Stream CloudWatch Logs

```bash
aws logs tail /ecs/serve-analyze-dev --follow --filter-pattern "berkley"
```

**Key Log Messages:**
```
[2025-10-10 14:32:10] Downloading data from S3: s3://serve-analyze-data-dev/input/berkley/1234567890.csv
[2025-10-10 14:32:15] Download complete. Files in /app/data/
[2025-10-10 14:32:20] Starting pipeline execution...
[2025-10-10 14:32:25] Stage 1: Consolidating messages...
[2025-10-10 14:35:10] Stage 2: Classifying messages...
[2025-10-10 14:38:45] Stage 3: Clustering analysis...
[2025-10-10 14:42:20] Stage 4: Uploading to DynamoDB...
[2025-10-10 14:43:05] Pipeline completed successfully!
[2025-10-10 14:43:10] Uploading results to S3: s3://serve-analyze-data-dev/output/berkley/1234567890/
[2025-10-10 14:43:25] Upload complete!
```

### Option 3: EventBridge Notifications (Future)

Set up EventBridge rule to trigger SNS notification on task completion:

```hcl
resource "aws_cloudwatch_event_rule" "task_completed" {
  name        = "serve-analyze-task-completed"
  description = "Trigger when V1 pipeline task completes"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn    = [aws_ecs_cluster.pipeline.arn]
      lastStatus    = ["STOPPED"]
      desiredStatus = ["STOPPED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "sns" {
  rule      = aws_cloudwatch_event_rule.task_completed.name
  target_id = "SendToSNS"
  arn       = aws_sns_topic.pipeline_notifications.arn
}
```

## Common Usage Patterns

### Pattern 1: CSV Upload from Client

```javascript
const formData = new FormData();
formData.append('campaign', 'berkley');
formData.append('csvFile', file);

const response = await fetch('/v1/pipeline/run', {
  method: 'POST',
  body: formData
});

const { taskArn, outputS3Path } = await response.json();

// Poll for completion
const waitForCompletion = async (taskArn) => {
  while (true) {
    const task = await checkTaskStatus(taskArn);
    if (task.status === 'STOPPED') {
      if (task.stopCode === 'EssentialContainerExited:0') {
        return { success: true, outputPath: outputS3Path };
      } else {
        throw new Error(`Task failed: ${task.stopReason}`);
      }
    }
    await sleep(5000); // Poll every 5 seconds
  }
};
```

### Pattern 2: S3-to-S3 Processing

```bash
# Pre-upload CSV to S3
aws s3 cp campaign_data.csv s3://my-data-bucket/campaigns/berkley.csv

# Trigger pipeline with S3 path
curl -X POST https://api.example.com/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "campaign": "berkley",
    "csvS3Path": "s3://my-data-bucket/campaigns/berkley.csv"
  }'

# Results automatically uploaded to:
# s3://serve-analyze-data-dev/output/berkley/{timestamp}/
```

### Pattern 3: Batch Processing Multiple Campaigns

```python
import boto3
import json
import time

lambda_client = boto3.client('lambda')

campaigns = ['berkley', 'josh', 'cara']
tasks = []

for campaign in campaigns:
    response = lambda_client.invoke(
        FunctionName='serve-analyze-trigger-dev',
        InvocationType='RequestResponse',
        Payload=json.dumps({
            'body': json.dumps({
                'campaign': campaign,
                'csvS3Path': f's3://my-data-bucket/campaigns/{campaign}.csv'
            })
        })
    )

    result = json.loads(response['Payload'].read())
    body = json.loads(result['body'])
    tasks.append({
        'campaign': campaign,
        'taskArn': body['taskArn'],
        'outputPath': body['outputS3Path']
    })

print(f"Started {len(tasks)} pipeline tasks:")
for task in tasks:
    print(f"  {task['campaign']}: {task['taskArn']}")

# All tasks run in parallel on Fargate
# Total time ≈ longest individual task (not sum of all tasks)
```

## Cost Example

**Scenario**: Processing 1,000 messages for "berkley" campaign

**Resources:**
- Fargate: 4 vCPU, 16GB RAM, 10 minutes runtime
- S3: 1MB input CSV, 5MB output files
- DynamoDB: 1,000 write requests
- CloudWatch Logs: 50MB

**Costs (us-east-1):**
- Fargate: $0.04048/vCPU-hour × 4 × (10/60) = $0.027
- Fargate Memory: $0.004445/GB-hour × 16 × (10/60) = $0.012
- S3 PUT: $0.005/1000 × 10 = $0.00005
- S3 Storage (30 days): $0.023/GB × 0.006 = $0.00014
- DynamoDB Write: $1.25/million × 0.001 = $0.00125
- CloudWatch Logs: $0.50/GB × 0.05 = $0.025

**Total per run: ~$0.065 (~6.5 cents)**

**Monthly costs (100 runs)**: ~$6.50

**Idle costs when not running**: $0.00 ✅

## Troubleshooting

### Request Rejected by Lambda

**Error**: `"campaign is required"`
```bash
# Fix: Ensure campaign field is present
curl -X POST https://api.example.com/v1/pipeline/run \
  -d '{"campaign": "berkley", ...}'
```

**Error**: `"Either csvData or csvS3Path must be provided"`
```bash
# Fix: Provide CSV data inline or S3 path
curl -X POST https://api.example.com/v1/pipeline/run \
  -d '{"campaign": "berkley", "csvData": "..."}'
# OR
curl -X POST https://api.example.com/v1/pipeline/run \
  -d '{"campaign": "berkley", "csvS3Path": "s3://..."}'
```

### Task Fails to Start

```bash
# Check task stopped reason
aws ecs describe-tasks --cluster serve-analyze-dev --tasks $TASK_ARN \
  --query 'tasks[0].{Reason:stoppedReason,Containers:containers[0].reason}'

# Common reasons:
# - "CannotPullContainerError": ECR image not found or permissions issue
# - "ResourceInitializationError": Secrets Manager access denied
# - "TaskFailedToStart": Invalid task definition or networking issue
```

### Pipeline Fails During Execution

```bash
# Check CloudWatch logs for error details
aws logs filter-log-events \
  --log-group-name /ecs/serve-analyze-dev \
  --filter-pattern "ERROR" \
  --start-time $(date -u -d '30 minutes ago' +%s)000

# Common errors:
# - CSV file not found in S3
# - Invalid Gemini API key
# - DynamoDB write permission denied
# - Network timeout connecting to API
```

### Results Not in DynamoDB

```bash
# Check if TEST_MODE was enabled
aws logs filter-log-events \
  --log-group-name /ecs/serve-analyze-dev \
  --filter-pattern "TEST MODE"

# If TEST_MODE=true, DynamoDB upload is skipped
# Results only available in S3 output files
```
