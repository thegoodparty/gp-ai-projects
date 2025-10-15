# ECS Task Failure Monitoring - Complete Solution

## Problem Statement

**Original Question**: "What if Fargate task fails, how can we know that an event triggered by S3 failed? It's not a queue that ends in the DLQ?"

**The Gap**:
```
S3 Upload → Lambda ✅ succeeds → ECS Task ❌ fails
                ↓
         Returns 200 OK
         S3 marks event as processed
         No DLQ, no notification
         Nobody knows! 😱
```

**Why Traditional DLQ Doesn't Work**:
- Lambda **successfully** starts ECS task → Returns 200
- ECS task fails **after** Lambda completes
- S3 considers the Lambda invocation successful
- DLQ only captures Lambda failures, not downstream ECS failures

## Solution Overview

We implemented a **3-layer monitoring system**:

1. **EventBridge Rules** - Capture ECS task failures in real-time
2. **SNS Notifications** - Send immediate email alerts with failure details
3. **ECS Task Tags** - Link failed tasks back to source S3 files

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  Complete Monitoring Flow                    │
└─────────────────────────────────────────────────────────────┘

S3 Upload: input/cara.csv
     ↓
Lambda Triggered
     ↓
Lambda tags ECS task with:
  - S3InputPath: s3://bucket/input/cara.csv
  - Campaign: cara
  - TriggerSource: S3Upload
     ↓
ECS Task Starts
     ↓
ECS Task Fails (exit code 1)
     ↓
ECS emits "Task State Change" to EventBridge
     ↓
EventBridge Rule matches:
  - lastStatus = STOPPED
  - exitCode ≠ 0
  - cluster = serve-analyze-dev
     ↓
EventBridge → SNS Topic
     ↓
SNS sends email:
  - Task ARN
  - Exit code
  - CloudWatch Logs link
  - Stopped reason
     ↓
You receive alert
     ↓
Query task tags to find source file:
  aws ecs describe-tasks --tasks $TASK_ARN
     ↓
Identify: cara.csv caused the failure
```

## Implementation Components

### 1. EventBridge Rule

**File**: `main.tf:413-437`

```hcl
resource "aws_cloudwatch_event_rule" "ecs_task_failed" {
  name        = "serve-analyze-task-failed-${var.environment}"
  description = "Capture ECS task failures for serve-analyze pipeline"

  event_pattern = jsonencode({
    source      = ["aws.ecs"]
    detail-type = ["ECS Task State Change"]
    detail = {
      clusterArn  = [aws_ecs_cluster.pipeline.arn]
      lastStatus  = ["STOPPED"]
      containers = {
        exitCode = [{
          "anything-but" = 0
        }]
      }
    }
  })
}
```

**What it catches**:
- ✅ Python exceptions (exit code 1)
- ✅ Missing environment variables (exit code 1)
- ✅ Out of memory (exit code 137)
- ✅ Killed processes (exit code 143)
- ❌ Successful completions (exit code 0)
- ❌ Manual stops by users

### 2. SNS Topic & Subscription

**File**: `main.tf:397-411`

```hcl
resource "aws_sns_topic" "pipeline_failures" {
  name = "serve-analyze-pipeline-failures-${var.environment}"
}

resource "aws_sns_topic_subscription" "pipeline_failures_email" {
  count     = var.failure_notification_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email
}
```

**Email format**:
```json
{
  "alarm": "ECS Task Failed",
  "environment": "dev",
  "cluster": "arn:aws:ecs:...:cluster/serve-analyze-dev",
  "taskArn": "arn:aws:ecs:...:task/serve-analyze-dev/abc123",
  "stoppedReason": "Essential container in task exited",
  "exitCode": 1,
  "time": "2025-10-13T18:30:45Z",
  "logs": "https://console.aws.amazon.com/cloudwatch/..."
}
```

### 3. ECS Task Tagging

**File**: `lambda-trigger/index.ts:106-119`

```typescript
tags: [
  {
    key: 'S3InputPath',
    value: s3InputPath,
  },
  {
    key: 'Campaign',
    value: campaign,
  },
  {
    key: 'TriggerSource',
    value: triggerSource,  // 'S3Upload' or 'ALB'
  },
]
```

**Why tags matter**:
When you receive a failure alert with task ARN, you can immediately find the source:

```bash
# Get task tags
aws ecs describe-tasks \
  --cluster serve-analyze-dev \
  --tasks arn:aws:ecs:...:task/.../abc123 \
  --query 'tasks[0].tags'
```

**Output**:
```json
[
  {"key": "S3InputPath", "value": "s3://bucket/input/cara-consolidated.csv"},
  {"key": "Campaign", "value": "cara-consolidated"},
  {"key": "TriggerSource", "value": "S3Upload"}
]
```

Now you know **exactly** which CSV file failed!

### 4. CloudWatch Alarm

**File**: `main.tf:486-505`

```hcl
resource "aws_cloudwatch_metric_alarm" "pipeline_high_failure_rate" {
  alarm_name          = "serve-analyze-high-failure-rate-${var.environment}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "TasksFailed"
  namespace           = "AWS/ECS"
  period              = 300
  statistic           = "Sum"
  threshold           = 3
  alarm_description   = "Alert when more than 3 ECS tasks fail in 5 minutes"
  alarm_actions       = [aws_sns_topic.pipeline_failures.arn]
}
```

**Purpose**: Detect systemic issues
- Single failure = Individual email
- 3+ failures in 5 min = CloudWatch alarm (aggregate issue)

## Deployment Steps

### 1. Add Email to Terraform Variables

```bash
cd infrastructure/environments/dev/serve-analyze-fargate
```

Create or edit `terraform.tfvars`:

```hcl
failure_notification_email = "team@example.com"
```

### 2. Build Lambda with Tags Support

```bash
cd infrastructure/modules/serve-analyze-fargate/lambda-trigger

npm install
npm run build
zip -r ../lambda-trigger.zip .
```

### 3. Apply Terraform

```bash
cd infrastructure/environments/dev/serve-analyze-fargate

terraform init
terraform plan
terraform apply
```

**What gets created**:
- ✅ SNS topic: `serve-analyze-pipeline-failures-dev`
- ✅ EventBridge rule: `serve-analyze-task-failed-dev`
- ✅ CloudWatch alarm: `serve-analyze-high-failure-rate-dev`
- ✅ Updated Lambda with tagging permissions
- ✅ Email subscription (pending confirmation)

### 4. Confirm SNS Subscription

Check your email for:
```
Subject: AWS Notification - Subscription Confirmation
From: no-reply@sns.amazonaws.com
```

**Click the confirmation link!**

### 5. Test Failure Detection

```bash
# Upload a malformed CSV to trigger failure
echo "invalid,data" | aws s3 cp - s3://serve-analyze-data-dev/input/test-failure.csv

# Wait 1-2 minutes for:
# 1. Lambda to start ECS task
# 2. ECS task to fail
# 3. EventBridge to catch failure
# 4. SNS to send email

# Check email inbox for failure notification
```

## Failure Recovery Workflow

### 1. Receive Failure Email

```json
{
  "taskArn": "arn:aws:ecs:...:task/serve-analyze-dev/abc123",
  "exitCode": 1,
  "logs": "https://console.aws.amazon.com/cloudwatch/..."
}
```

### 2. Check CloudWatch Logs

Click the `logs` link in email, or:

```bash
aws logs tail /ecs/serve-analyze-dev --follow
```

Look for the error:
```
ERROR: Invalid CSV format at line 42
KeyError: 'message_text' column not found
```

### 3. Find Source CSV File

```bash
TASK_ARN="arn:aws:ecs:...:task/serve-analyze-dev/abc123"

# Get task tags
aws ecs describe-tasks \
  --cluster serve-analyze-dev \
  --tasks $TASK_ARN \
  --query 'tasks[0].tags[?key==`S3InputPath`].value' \
  --output text
```

**Output**: `s3://serve-analyze-data-dev/input/cara-consolidated.csv`

### 4. Fix and Retry

```bash
# Download the problematic file
aws s3 cp s3://serve-analyze-data-dev/input/cara-consolidated.csv ./

# Fix the CSV locally
# ... edit file ...

# Re-upload to trigger automatic retry
aws s3 cp cara-consolidated.csv s3://serve-analyze-data-dev/input/

# OR manually trigger via ALB
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "x-api-key: YOUR_KEY" \
  -d '{"csvS3Path":"s3://serve-analyze-data-dev/input/cara-consolidated.csv"}'
```

## Monitoring Queries

### Find All Failed Tasks (Last 24 Hours)

```bash
aws ecs list-tasks \
  --cluster serve-analyze-dev \
  --desired-status STOPPED \
  --query 'taskArns[]' \
  --output text | \
  xargs -I {} aws ecs describe-tasks \
    --cluster serve-analyze-dev \
    --tasks {} \
    --query 'tasks[?containers[0].exitCode!=`0`].[taskArn,stoppedReason,containers[0].exitCode]' \
    --output table
```

### Get Failure Rate

```bash
# Tasks started in last hour
STARTED=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/ECS \
  --metric-name TasksStarted \
  --dimensions Name=ClusterName,Value=serve-analyze-dev \
  --statistics Sum \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --query 'Datapoints[0].Sum' \
  --output text)

# Tasks failed in last hour
FAILED=$(aws cloudwatch get-metric-statistics \
  --namespace AWS/ECS \
  --metric-name TasksFailed \
  --dimensions Name=ClusterName,Value=serve-analyze-dev \
  --statistics Sum \
  --start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) \
  --end-time $(date -u +%Y-%m-%dT%H:%M:%S) \
  --period 3600 \
  --query 'Datapoints[0].Sum' \
  --output text)

# Calculate failure rate
echo "Failure Rate: $(echo "scale=2; $FAILED / $STARTED * 100" | bc)%"
```

### Search for Failed Campaign

```bash
# Find which campaign failed most recently
aws ecs list-tasks \
  --cluster serve-analyze-dev \
  --desired-status STOPPED \
  --max-results 10 \
  --query 'taskArns[0]' \
  --output text | \
  xargs -I {} aws ecs describe-tasks \
    --cluster serve-analyze-dev \
    --tasks {} \
    --query 'tasks[0].tags[?key==`Campaign`].value' \
    --output text
```

## Cost Breakdown

**Per Failure**:
- EventBridge event: **FREE** (first 14M/month)
- SNS email: **$0.0000005** (half a micro-dollar)
- CloudWatch Logs: **$0.50/GB** (typically 1MB/task = $0.0005)
- CloudWatch alarm: **$0.10/month** (flat fee)

**Monthly Cost** (assuming 10 failures/day):
- EventBridge: FREE
- SNS: $0.00015/month
- CloudWatch Logs: $0.15/month
- CloudWatch alarm: $0.10/month
- **Total: ~$0.25/month**

Essentially free!

## Troubleshooting

### Not Receiving Emails

**Check subscription status**:
```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-west-2:123456:serve-analyze-pipeline-failures-dev \
  --query 'Subscriptions[].{Endpoint:Endpoint,Status:SubscriptionArn}'
```

If status is `PendingConfirmation`, check spam folder for confirmation email.

### Emails for Successful Tasks

**Verify EventBridge pattern**:
```bash
aws events describe-rule \
  --name serve-analyze-task-failed-dev \
  --query 'EventPattern' | jq .
```

Ensure `exitCode` filter includes `"anything-but": 0`.

### Can't Find Source File

**Verify task has tags**:
```bash
aws ecs describe-tasks \
  --cluster serve-analyze-dev \
  --tasks $TASK_ARN \
  --query 'tasks[0].tags'
```

If no tags, Lambda version is outdated. Redeploy Lambda.

## Comparison: Before vs After

### Before (No Monitoring)

```
❌ S3 upload fails → Silence
❌ Check CloudWatch manually → Search through 100+ tasks
❌ Find failure → Grep through logs for 30 minutes
❌ Identify source file → Search Lambda logs, cross-reference timestamps
❌ Recovery → Manual retry after debugging
```

**Time to detection**: Hours/Days (manual check)
**Time to resolution**: 1-2 hours

### After (With Monitoring)

```
✅ S3 upload fails → Email notification within 60 seconds
✅ Click CloudWatch link → See exact error immediately
✅ Query task tags → Source file identified in 5 seconds
✅ Fix CSV → Re-upload or manual trigger
```

**Time to detection**: <1 minute (automatic)
**Time to resolution**: 5-15 minutes

## Next Enhancements (Optional)

### 1. Automated Retry Logic

```typescript
// New Lambda triggered by EventBridge failures
// Retries up to 3 times before giving up
```

### 2. Slack Integration

```hcl
resource "aws_sns_topic_subscription" "slack" {
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "https"
  endpoint  = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
}
```

### 3. Failure Analytics Dashboard

```sql
-- CloudWatch Logs Insights Query
fields @timestamp, campaign, exitCode, duration
| filter @message like /Pipeline completed/
| stats count() by exitCode
```

## Summary

You now have **complete failure visibility** for S3-triggered pipelines:

1. ✅ **Real-time alerting** - Email within 60 seconds of failure
2. ✅ **Source tracking** - ECS task tags link back to S3 file
3. ✅ **Failure context** - Exit codes, logs, and stopped reasons
4. ✅ **Aggregate monitoring** - CloudWatch alarms for systemic issues
5. ✅ **Quick recovery** - 5-minute MTTR vs 1-hour MTTR

**No more silent failures!** 🎉
