# S3-Triggered Pipeline Deployment Guide

## Overview

The serve-analyze pipeline now supports **dual trigger modes**:
1. **ALB (Application Load Balancer)** - Manual trigger via HTTP POST (existing)
2. **S3 Event** - Automatic trigger on CSV file upload (new)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Trigger Options                      │
└─────────────────────────────────────────────────────────────┘

Option 1: ALB Trigger (Manual)
  curl POST → ALB (with x-api-key header) → Lambda → ECS Fargate Task

Option 2: S3 Event Trigger (Automatic)
  CSV Upload → S3 Bucket → S3 Event → Lambda → ECS Fargate Task

┌─────────────────────────────────────────────────────────────┐
│                      Lambda Handler                          │
│  - Detects event type (ALB vs S3)                           │
│  - Extracts campaign info from S3 path or request body      │
│  - Launches ECS Fargate task with environment variables     │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                    ECS Fargate Task                          │
│  - Reads CSV from S3                                         │
│  - Runs pipeline (consolidate → classify → cluster → upload)│
│  - Writes results to S3 output/                             │
│  - Uploads to DynamoDB via API                               │
└─────────────────────────────────────────────────────────────┘
```

## ALB Architecture

The pipeline uses an Application Load Balancer (ALB) for HTTP-triggered execution:

- **ALB Endpoint**: `https://ai-dev.goodparty.org/serve/messages/*`
- **Authentication**: API key validation at ALB listener rule level
- **Target**: Lambda function via ALB target group
- **Path Pattern**: `/serve/messages/*`
- **Required Header**: `x-api-key` (validated before Lambda invocation)

**Security Flow**:
1. Request arrives at ALB
2. ALB checks `x-api-key` header against configured value
3. ✅ Valid key → Forward to Lambda
4. ❌ Invalid/missing key → Return 403 (Lambda never invoked)

## S3 Event Configuration

**Bucket**: `serve-analyze-data-{environment}`
**Event**: `s3:ObjectCreated:*`
**Filter**:
- Prefix: `input/`
- Suffix: `.csv`

**Example trigger path**:
```
s3://serve-analyze-data-dev/input/--cara-burnsville--consolidated.csv
```

## Deployment Steps

### 1. Build and Package Lambda

```bash
cd infrastructure/modules/serve-analyze-fargate/lambda-trigger

npm install
npm run build

zip -r ../lambda-trigger.zip .
```

### 2. Apply Terraform Configuration

```bash
cd infrastructure/environments/dev/serve-analyze-fargate

terraform init
terraform plan
terraform apply
```

**What gets created**:
- ✅ S3 bucket notification for `input/*.csv` files
- ✅ Lambda permission for S3 to invoke function
- ✅ Updated Lambda handler with dual event support

### 3. Verify S3 Event Configuration

```bash
aws s3api get-bucket-notification-configuration \
  --bucket serve-analyze-data-dev \
  --query 'LambdaFunctionConfigurations'
```

Expected output:
```json
[
  {
    "Id": "...",
    "LambdaFunctionArn": "arn:aws:lambda:us-west-2:...:function:serve-analyze-trigger-dev",
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {
      "Key": {
        "FilterRules": [
          {"Name": "prefix", "Value": "input/"},
          {"Name": "suffix", "Value": ".csv"}
        ]
      }
    }
  }
]
```

## Usage

### Option 1: ALB HTTP Trigger (Original Method)

```bash
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{
    "csvS3Path": "s3://serve-analyze-data-dev/input/--cara-burnsville--consolidated.csv",
    "environment": "production",
    "anonymizeKeywords": ["cara", "burnsville"]
  }'
```

**Note**: The ALB validates the `x-api-key` header at the load balancer level (listener rule). Invalid/missing API keys return 403 before reaching the Lambda.

**Response**:
```json
{
  "taskArn": "arn:aws:ecs:us-west-2:...:task/serve-analyze-dev/...",
  "campaign": "--cara-burnsville--consolidated",
  "inputS3Path": "s3://serve-analyze-data-dev/input/--cara-burnsville--consolidated.csv",
  "outputS3Path": "s3://serve-analyze-data-dev/output/--cara-burnsville--consolidated/1760400641298/",
  "message": "Pipeline task started successfully"
}
```

### Option 2: S3 Upload Trigger (New Method)

**Step 1: Upload CSV to S3**

```bash
aws s3 cp local-file.csv \
  s3://serve-analyze-data-dev/input/--campaign-name--consolidated.csv
```

**Step 2: Pipeline Automatically Starts**

The S3 event automatically triggers the Lambda, which:
- Extracts campaign name from filename: `--campaign-name--consolidated`
- Creates output path: `s3://serve-analyze-data-dev/output/--campaign-name--consolidated/{timestamp}/`
- Launches ECS Fargate task with default configuration:
  - `environment: production`
  - `testMode: false`
  - `skipClassification: false`
  - `skipClustering: false`

**Step 3: Monitor Execution**

```bash
aws ecs list-tasks --cluster serve-analyze-dev --query 'taskArns[]' --output text | \
  xargs -I {} aws ecs describe-tasks --cluster serve-analyze-dev --tasks {} \
  --query 'tasks[].{TaskArn:taskArn,Status:lastStatus,Started:startedAt}'
```

**View Logs**:
```bash
aws logs tail /ecs/serve-analyze-dev --follow
```

## Campaign Name Extraction

The Lambda automatically extracts the campaign name from the S3 file path:

**Examples**:
- `input/--cara-burnsville--consolidated.csv` → Campaign: `--cara-burnsville--consolidated`
- `input/berkley-r1.csv` → Campaign: `berkley-r1`
- `input/josh-consolidated.csv` → Campaign: `josh-consolidated`

## Configuration Options

### S3-Triggered Defaults

When triggered via S3 upload, the pipeline uses these defaults:
- `environment`: `production` (INFO-level logging)
- `testMode`: `false` (uploads to DynamoDB)
- `skipClassification`: `false`
- `skipClustering`: `false`
- `apiUrl`: `https://ai-dev.goodparty.org`

### ALB-Triggered Customization

When triggered via ALB HTTP request, you can override all options:
```json
{
  "csvS3Path": "s3://...",
  "campaign": "custom-campaign-name",
  "environment": "development",
  "testMode": true,
  "skipClassification": true,
  "skipClustering": false,
  "anonymizeKeywords": ["keyword1", "keyword2"],
  "apiUrl": "https://custom-api.example.com"
}
```

## Monitoring

### CloudWatch Logs

**Lambda Logs**:
```bash
aws logs tail /aws/lambda/serve-analyze-trigger-dev --follow
```

**ECS Task Logs**:
```bash
aws logs tail /ecs/serve-analyze-dev --follow
```

### CloudWatch Metrics

Key metrics to monitor:
- **ALB**: `AWS/ApplicationELB` → `RequestCount`, `TargetResponseTime`, `HTTPCode_Target_4XX_Count`
- **Lambda**: `AWS/Lambda` → `Invocations`, `Errors`, `Duration`
- **ECS**: `AWS/ECS` → `RunningTasksCount`, `CPUUtilization`, `MemoryUtilization`
- **S3**: `AWS/S3` → `AllRequests`, `BytesDownloaded`

## Troubleshooting

### ALB Returns 403 Forbidden

**Symptom**: `curl` returns 403 with `{"error":"Forbidden","message":"Invalid or missing API key"}`

**Solutions**:
1. **Verify API key header**:
```bash
curl -v -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "x-api-key: YOUR_KEY" \
  -d '{"csvS3Path":"s3://bucket/input/file.csv"}'
```

2. **Check ALB listener rule configuration**:
```bash
aws elbv2 describe-rules \
  --listener-arn $(aws elbv2 describe-listeners \
    --load-balancer-arn $(aws elbv2 describe-load-balancers \
      --names serve-messages-dev --query 'LoadBalancers[0].LoadBalancerArn' --output text) \
    --query 'Listeners[?Port==`443`].ListenerArn' --output text) \
  --query 'Rules[?Priority==`20`]'
```

3. **Verify API key matches Terraform variable** in `dev.tfvars`

### S3 Event Not Triggering Lambda

1. **Check S3 notification configuration**:
```bash
aws s3api get-bucket-notification-configuration \
  --bucket serve-analyze-data-dev
```

2. **Verify Lambda has S3 invoke permission**:
```bash
aws lambda get-policy \
  --function-name serve-analyze-trigger-dev \
  --query 'Policy' | jq
```

3. **Check file matches filter**:
   - Must be in `input/` prefix
   - Must have `.csv` suffix

### Lambda Invoked But No Task Started

1. **Check Lambda logs**:
```bash
aws logs tail /aws/lambda/serve-analyze-trigger-dev --since 10m
```

2. **Common issues**:
   - Incorrect task definition ARN
   - Insufficient IAM permissions for Lambda
   - Network configuration errors (subnets/security groups)

### Pipeline Fails During Execution

1. **Check ECS task logs**:
```bash
aws ecs list-tasks --cluster serve-analyze-dev --query 'taskArns[0]' --output text | \
  xargs -I {} aws logs tail /ecs/serve-analyze-dev --follow
```

2. **Common issues**:
   - Invalid CSV format
   - Missing API keys (GEMINI_API_KEY, SERVE_API_KEY)
   - S3 permissions issues
   - DynamoDB permissions issues

## Cost Implications

**S3-Triggered Execution**:
- Lambda invocations: $0.20 per 1M requests
- Lambda compute: $0.0000166667 per GB-second
- ECS Fargate: Same as API-triggered (~$0.50 per run)
- S3 event notifications: Free

**Typical Cost per S3 Upload**: ~$0.50-1.00 (same as API trigger)

## Security Considerations

1. **S3 bucket is not public** - Only Lambda can read from it
2. **Lambda has least-privilege IAM role** - Can only trigger ECS tasks
3. **ECS tasks run in private subnets** with NAT gateway egress
4. **API keys stored as environment variables** in task definition (consider AWS Secrets Manager for production)

## Next Steps

To enable S3-triggered execution in production:

1. Update Lambda code (already done)
2. Package Lambda: `npm run build && zip -r lambda-trigger.zip .`
3. Apply Terraform: `terraform apply`
4. Test with upload: `aws s3 cp test.csv s3://serve-analyze-data-dev/input/test.csv`
5. Monitor execution: `aws logs tail /aws/lambda/serve-analyze-trigger-dev --follow`
