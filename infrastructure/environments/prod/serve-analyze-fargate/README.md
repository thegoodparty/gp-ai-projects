# Production Environment: serve-analyze-fargate

Production deployment for the V1 Message Analysis Pipeline with ECS Fargate, Step Functions, and automatic retry.

## Architecture

```
S3 Upload → Lambda Trigger → Step Functions → ECS Fargate Task
                                  ↓
                            (Auto-retry 3x)
                                  ↓
                            DynamoDB Upload
                                  ↓
                         EventBridge + SNS Alerts
```

## Resources Created

- **S3 Bucket**: `serve-analyze-data-prod`
- **ECS Cluster**: `serve-analyze-prod`
- **Lambda Trigger**: `serve-analyze-trigger-prod`
- **Step Functions**: `serve-analyze-pipeline-prod`
- **SNS Topic**: `serve-analyze-pipeline-failures-prod`
- **CloudWatch Logs**: `/ecs/serve-analyze-prod`
- **DynamoDB Table**: `serve-message-v1-prod` (pipeline target)

## Prerequisites

### 1. Docker Image

Build and push the prod Docker image:

```bash
cd /Users/collinpark/work/gp-ai-projects

# Build for ARM64 (Fargate)
docker buildx build --platform linux/arm64 -t serve-analyze-prod -f serve/v1_pipeline/Dockerfile .

# Tag for ECR
docker tag serve-analyze-prod:latest 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod

# Login to ECR
AWS_PROFILE=work aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 333022194791.dkr.ecr.us-west-2.amazonaws.com

# Push to ECR
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
```

### 2. API Keys

Update `terraform.tfvars` with production API keys:

```hcl
gemini_api_key = "YOUR_PROD_GEMINI_API_KEY"
serve_api_key  = "YOUR_PROD_SERVE_API_KEY"
```

**Security Note**: These keys are stored in Terraform state. Consider using AWS Secrets Manager for enhanced security.

### 3. DynamoDB Table

The pipeline writes to `serve-message-v1-prod`. Ensure this table exists or will be created by the serve-message-api module.

Check if table exists:
```bash
AWS_PROFILE=work aws dynamodb describe-table --table-name serve-message-v1-prod --region us-west-2
```

### 4. Notifications (Optional)

Add Slack or email notifications:

```hcl
# terraform.tfvars
slack_webhook_url          = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
failure_notification_email = "alerts@goodparty.org"
```

## Deployment

### Step 1: Review Configuration

```bash
cd /Users/collinpark/work/gp-ai-projects/infrastructure/environments/prod/serve-analyze-fargate

# Verify terraform.tfvars has correct values
cat terraform.tfvars
```

### Step 2: Initialize Terraform

```bash
AWS_PROFILE=work terraform init
```

### Step 3: Plan Deployment

```bash
AWS_PROFILE=work terraform plan -out=tfplan
```

Review the plan carefully. Expected resources:
- 10 new resources (same as dev)
- 0 changes
- 0 deletions

### Step 4: Deploy Infrastructure

```bash
AWS_PROFILE=work terraform apply tfplan
```

### Step 5: Verify Deployment

```bash
# Check cluster
AWS_PROFILE=work aws ecs describe-clusters --clusters serve-analyze-prod --region us-west-2

# Check Lambda
AWS_PROFILE=work aws lambda get-function --function-name serve-analyze-trigger-prod --region us-west-2

# Check Step Functions
AWS_PROFILE=work aws stepfunctions describe-state-machine --state-machine-arn \
  arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod --region us-west-2

# Check S3 bucket
AWS_PROFILE=work aws s3 ls s3://serve-analyze-data-prod/
```

## Usage

### Trigger Pipeline via S3

```bash
# Upload CSV to trigger pipeline
AWS_PROFILE=work aws s3 cp campaign-data.csv s3://serve-analyze-data-prod/input/

# Monitor Step Functions execution
AWS_PROFILE=work aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod \
  --region us-west-2
```

### Trigger Pipeline via HTTP (ALB)

```bash
curl -X POST https://ai.goodparty.org/serve/messages/process \
  -H "x-api-key: YOUR_PROD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "campaign": "campaign-name",
    "csvS3Path": "s3://serve-analyze-data-prod/input/campaign-data.csv",
    "environment": "production"
  }'
```

## Monitoring

### CloudWatch Logs

```bash
# View ECS task logs
AWS_PROFILE=work aws logs tail /ecs/serve-analyze-prod --follow --region us-west-2

# View Lambda logs
AWS_PROFILE=work aws logs tail /aws/lambda/serve-analyze-trigger-prod --follow --region us-west-2
```

### Step Functions Execution History

```bash
# Get recent executions
AWS_PROFILE=work aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-prod \
  --max-results 10 \
  --region us-west-2

# Get execution details
AWS_PROFILE=work aws stepfunctions describe-execution \
  --execution-arn EXECUTION_ARN \
  --region us-west-2
```

### S3 Output Files

```bash
# List pipeline outputs
AWS_PROFILE=work aws s3 ls s3://serve-analyze-data-prod/output/ --recursive --human-readable
```

## Automatic Retry

Step Functions automatically retries failed tasks:
- **Attempt 1**: Immediate
- **Attempt 2**: Wait 60 seconds
- **Attempt 3**: Wait 120 seconds
- **Attempt 4**: Wait 240 seconds (final)

After 4 failed attempts, SNS notification sent to configured endpoints.

## Cost Estimation

Production costs (approximate):
- **ECS Fargate**: $0.04048/vCPU/hour + $0.004445/GB/hour
  - 4 vCPU + 16GB RAM = ~$0.23/hour per task
  - Example: 10 pipelines/day, 30min each = $1.15/day = $35/month
- **Step Functions**: $0.025 per 1,000 state transitions
- **Lambda**: Negligible (brief invocations)
- **S3**: $0.023/GB/month storage + $0.005/1,000 requests
- **DynamoDB**: On-demand pricing (varies by usage)

## Troubleshooting

### Pipeline Fails Immediately

Check Lambda logs:
```bash
AWS_PROFILE=work aws logs tail /aws/lambda/serve-analyze-trigger-prod --follow --region us-west-2
```

### Task Fails During Execution

Check ECS logs:
```bash
AWS_PROFILE=work aws logs tail /ecs/serve-analyze-prod --follow --region us-west-2
```

### No S3 Trigger

Verify S3 notification:
```bash
AWS_PROFILE=work aws s3api get-bucket-notification-configuration --bucket serve-analyze-data-prod --region us-west-2
```

### Permission Errors

Check IAM policies for:
- Lambda execution role
- Step Functions execution role
- ECS task role

## Rollback

To rollback the deployment:

```bash
AWS_PROFILE=work terraform destroy
```

**Warning**: This will delete all resources including the S3 bucket (if empty) and CloudWatch logs.

## Differences from Dev

| Resource | Dev | Prod |
|----------|-----|------|
| Environment | `dev` | `prod` |
| S3 Bucket | `serve-analyze-data-dev` | `serve-analyze-data-prod` |
| Docker Tag | `serve-analyze-dev` | `serve-analyze-prod` |
| DynamoDB Table | `serve-message-v1-dev` | `serve-message-v1-prod` |
| API URL | `https://ai-dev.goodparty.org` | `https://ai.goodparty.org` |
| VPC | Dev VPC | Prod VPC (vpc-0763fa52c32ebcf6a) |
| Subnets | Dev private subnets | Prod private subnets |

## Support

For issues or questions:
1. Check CloudWatch Logs
2. Review Step Functions execution history
3. Verify API keys and permissions
4. Contact DevOps team
