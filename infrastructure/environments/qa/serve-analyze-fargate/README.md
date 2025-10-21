# QA Environment: serve-analyze-fargate

Quality Assurance environment for testing the V1 Message Analysis Pipeline before production deployment.

## Purpose

QA environment serves as the final testing stage before production:
- ✅ Integration testing with production-like configuration
- ✅ Performance testing at scale
- ✅ User acceptance testing (UAT)
- ✅ Regression testing for bug fixes
- ✅ Isolated from dev instability and prod risk

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

Same architecture as dev/prod, but with QA-specific resources.

## Resources Created

- **S3 Bucket**: `serve-analyze-data-qa`
- **ECS Cluster**: `serve-analyze-qa`
- **Lambda Trigger**: `serve-analyze-trigger-qa`
- **Step Functions**: `serve-analyze-pipeline-qa`
- **SNS Topic**: `serve-analyze-pipeline-failures-qa`
- **CloudWatch Logs**: `/ecs/serve-analyze-qa`
- **DynamoDB Table**: `serve-message-v1-qa` (target table)

## Network Configuration

### Option 1: Share Dev VPC (Recommended)
**Advantages:**
- Cost savings (no separate NAT gateway)
- Faster setup
- Good for testing non-production workloads

**Configuration:**
```hcl
vpc_id = "vpc-01fed488c4047eaae"  # Dev VPC
private_subnet_ids = [
  "subnet-05f87b096f980d9f5",
  "subnet-019e5237038e1fbb5",
  "subnet-0e92d5cf0848b2567",
  "subnet-0690eb3c1470d4163"
]
```

### Option 2: Use Prod VPC
**Advantages:**
- Mirrors production networking
- Better isolation from dev
- More realistic performance testing

**Configuration:**
```hcl
vpc_id = "vpc-0763fa52c32ebcf6a"  # Prod VPC
private_subnet_ids = [
  "subnet-053357b931f0524d4",
  "subnet-0bb591861f72dcb7f"
]
```

### Option 3: Dedicated QA VPC
**Advantages:**
- Complete isolation
- Independent security rules
- No impact from dev/prod

**Requirements:**
- Create new VPC infrastructure
- Additional networking costs

## Prerequisites

### 1. Docker Image

Build and push the QA Docker image:

```bash
cd /Users/collinpark/work/gp-ai-projects

# Build for ARM64 (Fargate)
docker buildx build --platform linux/arm64 -t serve-analyze-qa -f serve/v1_pipeline/Dockerfile .

# Tag for ECR
docker tag serve-analyze-qa:latest 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa

# Login to ECR
AWS_PROFILE=work aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin 333022194791.dkr.ecr.us-west-2.amazonaws.com

# Push to ECR
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa
```

### 2. API Keys

Update `terraform.tfvars` with QA-specific API keys:

```hcl
gemini_api_key = "YOUR_QA_GEMINI_API_KEY"
serve_api_key  = "YOUR_QA_SERVE_API_KEY"
```

**Note**: QA can use same keys as dev, or separate keys for quota isolation.

### 3. DynamoDB Table

The pipeline writes to `serve-message-v1-qa`. Options:

**Option A**: Create new table (isolated testing)
```bash
# Create via serve-message-api module or manually
```

**Option B**: Use dev table (shared testing)
```hcl
# In main.tf, change:
dynamodb_table_name = "serve-message-v1-dev"
```

### 4. Notifications (Optional)

Configure QA-specific alerts:

```hcl
# terraform.tfvars
slack_webhook_url          = "https://hooks.slack.com/services/YOUR/QA/WEBHOOK"
failure_notification_email = "qa-alerts@goodparty.org"
```

## Deployment

### Step 1: Review Configuration

```bash
cd /Users/collinpark/work/gp-ai-projects/infrastructure/environments/qa/serve-analyze-fargate

# Verify terraform.tfvars
cat terraform.tfvars

# Choose VPC strategy (dev, prod, or dedicated)
vi terraform.tfvars
```

### Step 2: Initialize Terraform

```bash
AWS_PROFILE=work terraform init
```

### Step 3: Plan Deployment

```bash
AWS_PROFILE=work terraform plan -out=tfplan
```

Expected resources: 10 new resources

### Step 4: Deploy Infrastructure

```bash
AWS_PROFILE=work terraform apply tfplan
```

### Step 5: Verify Deployment

```bash
# Check cluster
AWS_PROFILE=work aws ecs describe-clusters --clusters serve-analyze-qa --region us-west-2

# Check Lambda
AWS_PROFILE=work aws lambda get-function --function-name serve-analyze-trigger-qa --region us-west-2

# Check Step Functions
AWS_PROFILE=work aws stepfunctions describe-state-machine \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
  --region us-west-2

# Check S3 bucket
AWS_PROFILE=work aws s3 ls | grep serve-analyze-data-qa
```

## Usage

### Trigger Pipeline via S3

```bash
# Upload test CSV
AWS_PROFILE=work aws s3 cp test-campaign.csv s3://serve-analyze-data-qa/input/

# Monitor execution
AWS_PROFILE=work aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
  --region us-west-2
```

### Trigger Pipeline via HTTP (if ALB configured)

```bash
curl -X POST https://ai-qa.goodparty.org/serve/messages/process \
  -H "x-api-key: YOUR_QA_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "campaign": "test-campaign",
    "csvS3Path": "s3://serve-analyze-data-qa/input/test-campaign.csv",
    "environment": "qa"
  }'
```

## Testing Workflows

### 1. Smoke Test (Quick Validation)

```bash
# Upload minimal test file
echo "phone_number,message_text,direction,timestamp" > smoke-test.csv
echo "+15551234567,Test message,inbound,2024-10-14T10:00:00Z" >> smoke-test.csv

AWS_PROFILE=work aws s3 cp smoke-test.csv s3://serve-analyze-data-qa/input/smoke-$(date +%s).csv

# Verify completion
AWS_PROFILE=work aws logs tail /ecs/serve-analyze-qa --follow
```

### 2. Integration Test (Full Pipeline)

```bash
# Upload realistic dataset (100-1000 messages)
AWS_PROFILE=work aws s3 cp integration-test.csv s3://serve-analyze-data-qa/input/

# Monitor all stages
watch -n 5 'AWS_PROFILE=work aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
  --max-results 5'
```

### 3. Load Test (Performance Validation)

```bash
# Upload large dataset (10K+ messages)
AWS_PROFILE=work aws s3 cp load-test.csv s3://serve-analyze-data-qa/input/

# Monitor resource usage
AWS_PROFILE=work aws cloudwatch get-metric-statistics \
  --namespace AWS/ECS \
  --metric-name CPUUtilization \
  --dimensions Name=ClusterName,Value=serve-analyze-qa \
  --start-time $(date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 300 \
  --statistics Average
```

### 4. Failure Test (Retry Validation)

```bash
# Upload invalid CSV to test retry logic
echo "invalid" > failure-test.csv
AWS_PROFILE=work aws s3 cp failure-test.csv s3://serve-analyze-data-qa/input/

# Monitor retry attempts in Step Functions console
```

## Monitoring

### CloudWatch Logs

```bash
# View ECS task logs
AWS_PROFILE=work aws logs tail /ecs/serve-analyze-qa --follow

# View Lambda logs
AWS_PROFILE=work aws logs tail /aws/lambda/serve-analyze-trigger-qa --follow
```

### Step Functions Execution History

```bash
# Get recent executions
AWS_PROFILE=work aws stepfunctions list-executions \
  --state-machine-arn arn:aws:states:us-west-2:333022194791:stateMachine:serve-analyze-pipeline-qa \
  --region us-west-2

# Get execution details
AWS_PROFILE=work aws stepfunctions describe-execution \
  --execution-arn EXECUTION_ARN
```

### DynamoDB Data Validation

```bash
# Count records
AWS_PROFILE=work aws dynamodb scan \
  --table-name serve-message-v1-qa \
  --select COUNT

# Sample records
AWS_PROFILE=work aws dynamodb scan \
  --table-name serve-message-v1-qa \
  --limit 5
```

## Promotion to Production

After successful QA testing:

1. **Tag Docker image as production-ready:**
```bash
docker pull 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa
docker tag 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-qa \
           333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
docker push 333022194791.dkr.ecr.us-west-2.amazonaws.com/gp-ai-projects:serve-analyze-prod
```

2. **Deploy to production:**
```bash
cd ../prod/serve-analyze-fargate
terraform apply
```

3. **Run smoke tests in production**

## Cost Management

QA costs should be lower than prod:
- Run tests during business hours only
- Use dev VPC to save on networking costs
- Clean up old S3 files regularly
- Terminate idle resources

Estimated monthly cost: $30-80 (with moderate testing)

## Rollback

If QA environment has issues:

```bash
AWS_PROFILE=work terraform destroy
```

## Differences from Dev/Prod

| Configuration | Dev | QA | Prod |
|---------------|-----|----|----|
| **Purpose** | Active development | Pre-prod testing | Live production |
| **Stability** | Unstable | Stable | Very stable |
| **Data** | Test data | Realistic test data | Real data |
| **Resources** | `-dev` suffix | `-qa` suffix | `-prod` suffix |
| **Cost** | Low | Medium | High |
| **Deployment Frequency** | High | Medium | Low |

## Support

For QA environment issues:
1. Check CloudWatch Logs
2. Review Step Functions execution history
3. Verify test data format
4. Compare with dev environment behavior
5. Escalate to DevOps if needed
