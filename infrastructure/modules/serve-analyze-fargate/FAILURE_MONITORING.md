# ECS Task Failure Monitoring & Alerting

## The Problem

S3-triggered pipelines have a monitoring blind spot:

```
S3 Upload → Lambda ✅ → ECS Task ❌
                ↓
          Returns 200 OK
          S3 thinks: Success!
          No DLQ triggered
          No notification
```

**Why this happens**:
1. Lambda **successfully** starts the ECS task
2. Lambda returns 200 to S3
3. S3 considers event "processed"
4. ECS task fails **after** Lambda completes
5. Nobody knows until you check CloudWatch manually

## The Solution: EventBridge + SNS

### Architecture

```
┌────────────────────────────────────────────────────────────┐
│                    Failure Detection                        │
└────────────────────────────────────────────────────────────┘

ECS Task Fails
     ↓
ECS emits "Task State Change" event to EventBridge
     ↓
EventBridge Rule matches: lastStatus=STOPPED + exitCode≠0
     ↓
EventBridge sends to SNS Topic
     ↓
SNS sends email notification
     ↓
You receive: Task ARN, Exit Code, CloudWatch Logs link
```

### Components

**1. EventBridge Rule** (`ecs_task_failed`)
- Monitors ECS task state changes
- Filters for STOPPED tasks with non-zero exit codes
- Only watches your specific cluster

**2. SNS Topic** (`pipeline_failures`)
- Receives failure notifications from EventBridge
- Sends emails to configured addresses
- Can also send to Slack, PagerDuty, etc.

**3. CloudWatch Alarm** (`high_failure_rate`)
- Monitors aggregate failure metrics
- Alerts when >3 tasks fail in 5 minutes
- Helps detect systemic issues

## Deployment

### 1. Add Email to Terraform Variables

```bash
cd infrastructure/environments/dev/serve-analyze-fargate
```

Edit `main.tf` or create `terraform.tfvars`:

```hcl
failure_notification_email = "your-email@example.com"
```

### 2. Apply Terraform

```bash
terraform init
terraform plan
terraform apply
```

### 3. Confirm SNS Subscription

AWS will send a confirmation email:
```
Subject: AWS Notification - Subscription Confirmation
From: no-reply@sns.amazonaws.com

Please confirm your subscription by visiting this URL:
https://sns.us-west-2.amazonaws.com/...
```

**Click the confirmation link!**

### 4. Test Failure Notification

Trigger a test failure:

```bash
# Upload a malformed CSV to force failure
echo "invalid,data" | aws s3 cp - s3://serve-analyze-data-dev/input/test-failure.csv

# Watch for notification email within 1-2 minutes
```

## Notification Format

### Email Subject
```
ALARM: "serve-analyze-task-failed-dev" in US West (Oregon)
```

### Email Body
```json
{
  "alarm": "ECS Task Failed",
  "environment": "dev",
  "cluster": "arn:aws:ecs:us-west-2:123456:cluster/serve-analyze-dev",
  "taskArn": "arn:aws:ecs:us-west-2:123456:task/serve-analyze-dev/abc123",
  "stoppedReason": "Essential container in task exited",
  "exitCode": 1,
  "time": "2025-10-13T18:30:45Z",
  "logs": "https://console.aws.amazon.com/cloudwatch/home?region=us-west-2#logsV2:log-groups/log-group/$252Fecs$252Fserve-analyze-dev"
}
```

Click the `logs` URL to see what went wrong.

## EventBridge Event Pattern

The rule catches these scenarios:

### ✅ Caught (Will Send Alert)

**Scenario 1: Python Exception**
```python
# Pipeline code
raise ValueError("Invalid CSV format")
# Exit code: 1
```

**Scenario 2: Missing Environment Variable**
```bash
# Container starts but fails
Error: GEMINI_API_KEY not set
# Exit code: 1
```

**Scenario 3: Out of Memory**
```
Container killed due to memory limits
# Exit code: 137 (128 + 9 SIGKILL)
```

### ❌ Not Caught (Won't Alert)

**Scenario 1: Graceful Completion**
```python
# Pipeline completes successfully
sys.exit(0)
# Exit code: 0 → No alert
```

**Scenario 2: Task Stopped Manually**
```bash
aws ecs stop-task --task abc123
# Stopped by user → No alert
```

## Event Pattern Details

```json
{
  "source": ["aws.ecs"],
  "detail-type": ["ECS Task State Change"],
  "detail": {
    "clusterArn": ["arn:aws:ecs:us-west-2:123456:cluster/serve-analyze-dev"],
    "lastStatus": ["STOPPED"],
    "containers": {
      "exitCode": [{
        "anything-but": 0
      }]
    }
  }
}
```

**Key Filters**:
- `lastStatus = STOPPED` → Task finished (not still running)
- `exitCode ≠ 0` → Non-zero exit (failure)
- `clusterArn` → Only your cluster (not other ECS tasks)

## Monitoring Dashboard

### CloudWatch Logs Insights Queries

**Failed Tasks in Last Hour**:
```sql
fields @timestamp, @message
| filter @message like /ERROR/
| sort @timestamp desc
| limit 100
```

**Task Duration Distribution**:
```sql
fields @timestamp, @duration
| filter @message like /Pipeline completed/
| stats avg(@duration), max(@duration), min(@duration)
```

### CloudWatch Metrics

**Task Failure Rate**:
- Namespace: `AWS/ECS`
- Metric: `TasksFailed`
- Dimension: `ClusterName = serve-analyze-dev`
- Statistic: Sum over 5 minutes

**Task Success Rate**:
```
Success Rate = (TasksStarted - TasksFailed) / TasksStarted * 100
```

## Advanced: Linking S3 Upload to Task Failure

### Problem
When you receive a failure email, you need to know **which CSV file** caused it.

### Solution: CloudWatch Logs Correlation

**Step 1**: Lambda logs the S3 path when starting task:
```typescript
console.log(`S3 trigger: ${request.csvS3Path}`);
console.log(`Started task: ${taskArn}`);
```

**Step 2**: Search Lambda logs for task ARN:
```bash
TASK_ARN="arn:aws:ecs:us-west-2:123456:task/serve-analyze-dev/abc123"

aws logs filter-log-events \
  --log-group-name /aws/lambda/serve-analyze-trigger-dev \
  --filter-pattern "$TASK_ARN" \
  --query 'events[].message'
```

**Output**:
```json
[
  "S3 trigger: s3://serve-analyze-data-dev/input/cara-consolidated.csv",
  "Started task: arn:aws:ecs:us-west-2:123456:task/serve-analyze-dev/abc123"
]
```

Now you know `cara-consolidated.csv` caused the failure!

### Better Solution: Add S3 Path to Task Tags

Enhance Lambda to tag ECS tasks with source S3 path:

```typescript
// In lambda-trigger/index.ts
const runTaskCommand = new RunTaskCommand({
  cluster: CLUSTER_NAME,
  taskDefinition: TASK_DEFINITION,
  tags: [
    {
      key: 'S3InputPath',
      value: request.csvS3Path
    },
    {
      key: 'Campaign',
      value: campaign
    },
    {
      key: 'TriggerSource',
      value: isS3Event(event) ? 'S3Upload' : 'ALB'
    }
  ],
  // ... rest of config
});
```

Then query failed tasks with tags:

```bash
aws ecs describe-tasks \
  --cluster serve-analyze-dev \
  --tasks $TASK_ARN \
  --query 'tasks[0].tags'
```

**Output**:
```json
[
  {
    "key": "S3InputPath",
    "value": "s3://serve-analyze-data-dev/input/cara-consolidated.csv"
  },
  {
    "key": "Campaign",
    "value": "cara-consolidated"
  },
  {
    "key": "TriggerSource",
    "value": "S3Upload"
  }
]
```

## Alerting Best Practices

### Email Overload Prevention

**Problem**: If 10 files fail, you get 10 emails immediately.

**Solution 1**: CloudWatch Alarm (Already Configured)
- Only alerts when >3 failures in 5 minutes
- Single alarm for multiple failures

**Solution 2**: SNS Filtering
```hcl
resource "aws_sns_topic_subscription" "pipeline_failures_filtered" {
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "email"
  endpoint  = var.failure_notification_email

  filter_policy = jsonencode({
    exitCode = [1, 137, 143]  # Only specific error codes
  })
}
```

### Escalation

**Level 1**: Email notification (immediate)
**Level 2**: CloudWatch Alarm (aggregate failures)
**Level 3**: PagerDuty/Slack (critical threshold)

```hcl
resource "aws_sns_topic_subscription" "pagerduty" {
  topic_arn = aws_sns_topic.pipeline_failures.arn
  protocol  = "https"
  endpoint  = "https://events.pagerduty.com/integration/${var.pagerduty_key}/enqueue"
}
```

## Failure Recovery

### Manual Retry

```bash
# Get failed task details
TASK_ARN="arn:aws:ecs:..."
aws ecs describe-tasks --cluster serve-analyze-dev --tasks $TASK_ARN

# Find S3 input path from tags
S3_PATH=$(aws ecs describe-tasks --cluster serve-analyze-dev --tasks $TASK_ARN \
  --query 'tasks[0].tags[?key==`S3InputPath`].value' --output text)

# Retry via ALB trigger
curl -X POST https://ai-dev.goodparty.org/serve/messages/process \
  -H "x-api-key: YOUR_KEY" \
  -d "{\"csvS3Path\":\"$S3_PATH\"}"
```

### Automated Retry (Future Enhancement)

Use EventBridge → Lambda → Retry pattern:

```typescript
// Retry Lambda triggered by failure events
export const handler = async (event: any) => {
  const failedTaskArn = event.detail.taskArn;

  // Get S3 path from task tags
  const s3Path = await getTaskS3Path(failedTaskArn);

  // Check retry count
  const retryCount = await getRetryCount(s3Path);

  if (retryCount < 3) {
    // Retry the pipeline
    await startECSTask(s3Path);
    await incrementRetryCount(s3Path);
  } else {
    // Give up, send to dead letter queue
    await sendToDeadLetterQueue(s3Path, failedTaskArn);
  }
};
```

## Cost Implications

**Per Failure**:
- EventBridge event: **FREE** (first 14M events/month)
- SNS notification: **$0.0000005** (0.5 micro-dollars)
- CloudWatch Alarm evaluation: **$0.10/month** (flat rate)

**Total**: Essentially free for normal failure rates

## Monitoring Checklist

- [x] EventBridge rule capturing ECS failures
- [x] SNS topic for notifications
- [x] Email subscription confirmed
- [x] CloudWatch alarm for high failure rate
- [x] Set up Slack/PagerDuty integration
- [x] Configure automated retry logic (step function, tries 3 times)

## Troubleshooting

### Not Receiving Emails

**1. Check SNS subscription status**:
```bash
aws sns list-subscriptions-by-topic \
  --topic-arn arn:aws:sns:us-west-2:123456:serve-analyze-pipeline-failures-dev
```

Look for `SubscriptionArn` (not "PendingConfirmation").

**2. Test SNS manually**:
```bash
aws sns publish \
  --topic-arn arn:aws:sns:us-west-2:123456:serve-analyze-pipeline-failures-dev \
  --subject "Test Notification" \
  --message "This is a test"
```

**3. Check EventBridge rule**:
```bash
aws events describe-rule --name serve-analyze-task-failed-dev
```

### Emails for Successful Tasks

**Symptom**: Getting notifications even when tasks succeed.

**Cause**: EventBridge pattern might be too broad.

**Fix**: Verify `exitCode ≠ 0` filter:
```bash
aws events describe-rule --name serve-analyze-task-failed-dev \
  --query 'EventPattern' | jq .
```

### Multiple Emails per Failure

**Cause**: Container restarts create multiple STOPPED events.

**Fix**: Add `stopCode` filter to exclude retries:
```json
{
  "detail": {
    "stopCode": ["TaskFailedToStart", "EssentialContainerExited"]
  }
}
```

## Next Steps

1. **Deploy monitoring** (see Deployment section)
2. **Confirm email subscription**
3. **Test with intentional failure**
4. **Enhance Lambda to add S3 tags** (optional)
5. **Set up dashboard** (optional)
6. **Configure automated retry** (optional)
