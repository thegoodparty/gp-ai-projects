# V1 Pipeline - Fargate Deployment

Complete AWS infrastructure for running the V1 Pipeline on Fargate with scale-to-zero capability.

## Architecture

```
┌─────────────────┐
│  API Gateway    │  POST /v1/pipeline/run
│   or Lambda     │  Body: { "campaign": "berkley", "csvData": "..." }
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  Trigger Lambda (pipeline-runner)                   │
│  - Validates request                                │
│  - Uploads CSV to S3 if provided                    │
│  - Calls ECS RunTask API with environment overrides │
│  - Returns task ARN for status tracking             │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────┐
│  ECS Fargate Task (scale-to-zero)                   │
│  - Downloads CSV from S3                            │
│  - Runs serve-analyze container with runtime params   │
│  - Processes: consolidate → classify → cluster      │
│  - Uploads results to DynamoDB                      │
│  - Exports CSV to S3 output bucket                  │
│  - Task automatically terminates when done          │
└────────┬────────────────────────────────────────────┘
         │
         ▼
┌─────────────────┐
│   DynamoDB      │  Results stored for API consumption
└─────────────────┘
```

## Components

### 1. S3 Bucket (`aws_s3_bucket.pipeline_data`)
- **input/**: Campaign CSV files (30-day lifecycle)
- **output/**: Generated results (archived to Glacier after 7 days)

### 2. ECS Fargate Task (`aws_ecs_task_definition.pipeline`)
- **CPU**: 4 vCPUs (4096)
- **Memory**: 16 GB (16384)
- **Network**: awsvpc mode with private subnets
- **Scale-to-zero**: Tasks only run when triggered, no idle costs

### 3. Lambda Trigger Function (`aws_lambda_function.pipeline_trigger`)
- Receives API requests
- Uploads CSV data to S3
- Triggers ECS Fargate task with runtime parameters
- Returns task ARN for status tracking

### 4. IAM Roles
- **Task Execution Role**: Pull secrets, write CloudWatch logs
- **Task Role**: Read/write S3, write to DynamoDB
- **Lambda Role**: Trigger ECS tasks, write to S3

## Request Flow

### Trigger the Pipeline

```bash
curl -X POST https://api.example.com/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "campaign": "berkley",
    "csvData": "round,phone,message,sentiment\n1,+1234567890,Hello,positive\n...",
    "testMode": false,
    "apiUrl": "https://ai-dev.goodparty.org",
    "environment": "production"
  }'
```

**Response:**
```json
{
  "taskArn": "arn:aws:ecs:us-east-1:123456789:task/serve-analyze-prod/abc123",
  "campaign": "berkley",
  "inputS3Path": "s3://serve-analyze-data-prod/input/berkley/1234567890.csv",
  "outputS3Path": "s3://serve-analyze-data-prod/output/berkley/1234567890/",
  "message": "Pipeline task started successfully"
}
```

### Alternative: Provide S3 Path Instead of CSV Data

```bash
curl -X POST https://api.example.com/v1/pipeline/run \
  -H "Content-Type: application/json" \
  -d '{
    "campaign": "berkley",
    "csvS3Path": "s3://my-bucket/campaigns/berkley_data.csv"
  }'
```

## Task Monitoring

### Check Task Status

```bash
aws ecs describe-tasks \
  --cluster serve-analyze-prod \
  --tasks arn:aws:ecs:us-east-1:123456789:task/serve-analyze-prod/abc123
```

### View Logs

```bash
aws logs tail /ecs/serve-analyze-prod --follow
```

### Get Results from S3

```bash
aws s3 ls s3://serve-analyze-data-prod/output/berkley/1234567890/
aws s3 cp s3://serve-analyze-data-prod/output/berkley/1234567890/ ./results/ --recursive
```

## Cost Optimization

### Scale-to-Zero Architecture
- **Idle Cost**: $0 (no running tasks when not in use)
- **Per-Task Cost**: Only pay for task execution time
- **Typical Task**: 4 vCPU, 16GB RAM, ~5-10 minutes = ~$0.10-0.20 per run

### Data Lifecycle
- Input files deleted after 30 days
- Output files archived to Glacier after 7 days (90% cost reduction)

## Deployment

### Prerequisites
1. Build and push Docker image to ECR
2. Create Secrets Manager secrets for API keys
3. Have existing VPC with private subnets
4. Have existing DynamoDB table

### Deploy Infrastructure

```bash
cd infrastructure/modules/serve-analyze-fargate

terraform init

terraform plan \
  -var="environment=prod" \
  -var="vpc_id=vpc-xxx" \
  -var="private_subnet_ids=[subnet-xxx,subnet-yyy]" \
  -var="docker_image=123456789.dkr.ecr.us-east-1.amazonaws.com/serve-analyze:latest" \
  -var="gemini_api_key_secret_arn=arn:aws:secretsmanager:xxx" \
  -var="api_key_secret_arn=arn:aws:secretsmanager:xxx" \
  -var="dynamodb_table_name=serve-message-v1-prod"

terraform apply
```

### Build and Push Docker Image

```bash
# Build image
cd serve/serve_analyze
./build.sh prod

# Tag for ECR
docker tag serve-analyze:prod 123456789.dkr.ecr.us-east-1.amazonaws.com/serve-analyze:latest

# Login to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 123456789.dkr.ecr.us-east-1.amazonaws.com

# Push to ECR
docker push 123456789.dkr.ecr.us-east-1.amazonaws.com/serve-analyze:latest
```

### Package and Deploy Lambda

```bash
cd infrastructure/modules/serve-analyze-fargate/lambda-trigger

# Install dependencies
npm install

# Package for Lambda
zip -r ../lambda-trigger.zip .

# Deploy via Terraform (will pick up the zip file)
cd ..
terraform apply
```

## Environment Variables

### Required (from Secrets Manager)
- `GEMINI_API_KEY` - Google Gemini API key
- `API_KEY` - Serve Messages API key

### Optional (passed at runtime)
- `CAMPAIGN_NAME` - Campaign identifier (required)
- `S3_INPUT_PATH` - S3 path to input CSV
- `S3_OUTPUT_PATH` - S3 path for output files
- `API_URL` - Serve Messages API URL (default: https://ai-dev.goodparty.org)
- `ENVIRONMENT` - Environment name (default: production)
- `TEST_MODE` - Skip DynamoDB upload (default: false)
- `SKIP_CLASSIFICATION` - Skip classification stage (default: false)
- `SKIP_CLUSTERING` - Skip clustering stage (default: false)

## Security

> 📖 **For detailed security improvements and fixes, see [SECURITY_IMPROVEMENTS.md](./SECURITY_IMPROVEMENTS.md)**

### Network Isolation
- ECS tasks run in private subnets with no public IPs (AssignPublicIp: DISABLED)
- NAT Gateway provides outbound-only internet access
- Security group allows only outbound traffic

### Data Protection
- S3 bucket encryption enabled (AES256) with S3 Bucket Keys
- Public access completely blocked on all S3 buckets
- Secrets encrypted at rest in AWS Secrets Manager

### Secrets Management
- API keys stored in AWS Secrets Manager (never in code/state)
- Task execution role grants access to specific secrets only
- Secrets injected as environment variables at runtime

### IAM Least Privilege
- Task role: S3 read (input), S3 write (output), DynamoDB write (scoped to specific table)
- Step Functions role: ECS actions scoped to specific cluster and task definitions only
- Lambda role: Step Functions execution scoped to specific state machine
- Execution role: Secrets Manager read, CloudWatch Logs write

## Troubleshooting

### Task Fails to Start
```bash
# Check task stopped reason
aws ecs describe-tasks --cluster serve-analyze-prod --tasks TASK_ARN \
  --query 'tasks[0].stoppedReason'

# Common issues:
# - Missing secrets in Secrets Manager
# - Invalid subnet/security group configuration
# - Insufficient IAM permissions
```

### Task Runs but Pipeline Fails
```bash
# Check CloudWatch logs
aws logs tail /ecs/serve-analyze-prod --follow

# Common issues:
# - Missing CSV file in S3
# - Invalid CAMPAIGN_NAME
# - API key issues (check DynamoDB write permissions)
```

### S3 Upload/Download Issues
```bash
# Verify IAM permissions
aws iam get-role-policy --role-name serve-analyze-task-prod --policy-name s3-access

# Test S3 access from local Docker
docker run --rm \
  -e AWS_ACCESS_KEY_ID=xxx \
  -e AWS_SECRET_ACCESS_KEY=xxx \
  -e S3_INPUT_PATH=s3://bucket/file.csv \
  serve-analyze:latest
```

## Monitoring

### CloudWatch Metrics
- Task count, CPU utilization, memory utilization
- Container Insights enabled for detailed metrics

### CloudWatch Alarms (recommended)
```hcl
resource "aws_cloudwatch_metric_alarm" "task_failed" {
  alarm_name          = "serve-analyze-task-failed"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "TasksStoppedReason"
  namespace           = "AWS/ECS"
  period              = "300"
  statistic           = "Sum"
  threshold           = "0"
  alarm_description   = "Alert when ECS task fails"

  dimensions = {
    ClusterName = aws_ecs_cluster.pipeline.name
  }
}
```

## Future Enhancements

- [ ] API Gateway integration for direct HTTP access
- [ ] EventBridge rule for scheduled pipeline runs
- [ ] SNS notifications for task completion/failure
- [ ] Step Functions orchestration for multi-campaign batch processing
- [ ] CloudWatch Dashboard for pipeline metrics
- [ ] Auto-scaling based on SQS queue depth
