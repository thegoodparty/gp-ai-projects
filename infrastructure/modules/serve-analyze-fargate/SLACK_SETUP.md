# Slack Integration for ECS Task Failure Alerts

## Overview

Instead of email notifications, you can send beautifully formatted failure alerts directly to a Slack channel:

```
🚨 ECS Task Failed
━━━━━━━━━━━━━━━━━━━━━
Environment: dev
Task ID: abc12345
Exit Code: 1
Time: Oct 13, 2025 6:30 PM
━━━━━━━━━━━━━━━━━━━━━
Stopped Reason: Essential container in task exited

[View Logs] (clickable button)
```

## Architecture

```
ECS Task Fails
     ↓
EventBridge Rule → SNS Topic
     ↓
Lambda Function (slack-notifier)
     ↓
Formats message for Slack
     ↓
POST to Slack Webhook URL
     ↓
Message appears in #pipeline-alerts
```

**Benefits over Email**:
- ✅ Rich formatting with colors and buttons
- ✅ Thread discussions right in Slack
- ✅ Mobile push notifications
- ✅ No email clutter
- ✅ Team visibility
- ✅ One-click log access

## Setup Steps

### 1. Create Slack Webhook

**Step 1**: Go to your Slack workspace → Apps

**Step 2**: Search for "Incoming Webhooks" or visit:
```
https://YOUR_WORKSPACE.slack.com/apps/A0F7XDUAZ-incoming-webhooks
```

**Step 3**: Click "Add to Slack"

**Step 4**: Choose a channel (e.g., `#pipeline-alerts`)

**Step 5**: Copy the Webhook URL:
```
https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX
```

**IMPORTANT**: Keep this URL secret! It allows posting to your Slack workspace.

### 2. Build Slack Notifier Lambda

```bash
cd infrastructure/modules/serve-analyze-fargate/slack-notifier

# Install dependencies
npm install

# Build TypeScript
npm run build

# Create deployment package
cd dist
zip -r ../slack-notifier.zip .
cd ..

# Move to parent directory for Terraform
mv slack-notifier.zip ../
```

### 3. Configure Terraform

Edit `terraform.tfvars`:

```hcl
# Slack webhook URL
slack_webhook_url = "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX"

# Optional: Keep email as backup
failure_notification_email = "team@example.com"
```

**Note**: You can have **both** Slack and email notifications!

### 4. Deploy with Terraform

```bash
cd infrastructure/environments/dev/serve-analyze-fargate

terraform init
terraform plan
terraform apply
```

**What gets created**:
- ✅ Lambda function: `serve-analyze-slack-notifier-dev`
- ✅ IAM role for Lambda
- ✅ SNS subscription (Lambda)
- ✅ Lambda permission for SNS

### 5. Test the Integration

**Option A**: Trigger a test failure

```bash
# Upload invalid CSV
echo "invalid,data" | aws s3 cp - s3://serve-analyze-data-dev/input/test-failure.csv

# Wait 1-2 minutes, check Slack channel
```

**Option B**: Send test SNS message

```bash
aws sns publish \
  --topic-arn arn:aws:sns:us-west-2:123456:serve-analyze-pipeline-failures-dev \
  --message '{
    "alarm": "ECS Task Failed",
    "environment": "dev",
    "cluster": "arn:aws:ecs:us-west-2:123456:cluster/serve-analyze-dev",
    "taskArn": "arn:aws:ecs:us-west-2:123456:task/serve-analyze-dev/test123",
    "stoppedReason": "Test failure notification",
    "exitCode": 1,
    "time": "'$(date -u +%Y-%m-%dT%H:%M:%SZ)'",
    "logs": "https://console.aws.amazon.com/cloudwatch/home"
  }'
```

## Slack Message Format

### Color Coding

The Lambda automatically color-codes messages based on exit code:

| Exit Code | Color | Meaning |
|-----------|-------|---------|
| 1 | 🔴 Red | General error (Python exception, missing file, etc.) |
| 137 | 🟠 Orange | Out of Memory (OOM kill) |
| 143 | 🟡 Yellow | SIGTERM (graceful shutdown requested) |
| Other | 🔴 Dark Red | Unknown error |

### Message Fields

```
🚨 ECS Task Failed
━━━━━━━━━━━━━━━━━━━━━
Environment: dev              Task ID: abc12345
Exit Code: 1                  Time: Oct 13, 2025 6:30 PM
━━━━━━━━━━━━━━━━━━━━━
Stopped Reason: Essential container in task exited
━━━━━━━━━━━━━━━━━━━━━
[View Logs] ← Clickable button
```

**Fields**:
- **Environment**: `dev` or `prod`
- **Task ID**: First 8 characters of task ARN
- **Exit Code**: Container exit code
- **Time**: Timestamp of failure (localized)
- **Stopped Reason**: ECS stop reason
- **View Logs**: Direct link to CloudWatch Logs

### Interactive Features

- **Click "View Logs"** → Opens CloudWatch Logs in browser
- **Click timestamp** → Slack's timestamp hover shows full time
- **Reply in thread** → Team discussion about failure
- **React with emoji** → 👀 = investigating, ✅ = fixed

## Slack Message Code

The Lambda formats messages like this:

```typescript
{
  username: 'ECS Pipeline Monitor',
  icon_emoji: ':rotating_light:',
  attachments: [
    {
      color: '#FF0000',  // Red for exit code 1
      title: ':x: ECS Task Failed',
      title_link: 'https://console.aws.amazon.com/cloudwatch/...',
      fields: [
        { title: 'Environment', value: 'dev', short: true },
        { title: 'Task ID', value: 'abc12345', short: true },
        { title: 'Exit Code', value: '1', short: true },
        { title: 'Time', value: 'Oct 13, 2025 6:30 PM', short: true },
        { title: 'Stopped Reason', value: '...', short: false }
      ],
      actions: [
        { type: 'button', text: 'View Logs', url: '...', style: 'danger' }
      ]
    }
  ]
}
```

## Advanced Configuration

### Multiple Channels

Send critical failures to on-call channel:

```typescript
// In slack-notifier/index.ts
const CRITICAL_WEBHOOK = process.env.CRITICAL_WEBHOOK_URL;
const STANDARD_WEBHOOK = process.env.SLACK_WEBHOOK_URL;

function getWebhookUrl(exitCode: number): string {
  if (exitCode === 137 || exitCode === 143) {
    return CRITICAL_WEBHOOK;  // OOM or SIGTERM → #on-call
  }
  return STANDARD_WEBHOOK;  // Other errors → #pipeline-alerts
}
```

**Terraform**:
```hcl
variable "critical_webhook_url" {
  type      = string
  sensitive = true
}

environment {
  variables = {
    SLACK_WEBHOOK_URL    = var.slack_webhook_url
    CRITICAL_WEBHOOK_URL = var.critical_webhook_url
  }
}
```

### Custom Message Format

**Add campaign name to Slack message**:

```typescript
// Enhance formatSlackMessage()
fields: [
  {
    title: 'Campaign',
    value: message.campaign || 'Unknown',  // Add this field
    short: true
  },
  // ... other fields
]
```

**Query ECS task tags for campaign**:

```typescript
import { ECSClient, DescribeTasksCommand } from '@aws-sdk/client-ecs';

async function getCampaignFromTask(taskArn: string): Promise<string> {
  const ecs = new ECSClient({});
  const response = await ecs.send(new DescribeTasksCommand({
    cluster: 'serve-analyze-dev',
    tasks: [taskArn],
  }));

  const tags = response.tasks?.[0]?.tags || [];
  const campaignTag = tags.find(tag => tag.key === 'Campaign');
  return campaignTag?.value || 'Unknown';
}
```

### Mention On-Call Team

For critical failures, mention on-call:

```typescript
function formatSlackMessage(message: TaskFailureMessage): any {
  const isCritical = message.exitCode === 137 || message.exitCode === 143;

  return {
    text: isCritical ? '<!subteam^S1234567890> Critical ECS failure' : undefined,
    // ... rest of message
  };
}
```

Replace `S1234567890` with your Slack user group ID (find via Slack API).

### Throttling Alerts

Prevent spam during mass failures:

```typescript
// Add to Lambda
const Redis = require('redis');
const redis = Redis.createClient({ url: process.env.REDIS_URL });

async function shouldSendAlert(taskArn: string): Promise<boolean> {
  const key = `alert:${taskArn}`;
  const exists = await redis.exists(key);

  if (exists) {
    return false;  // Already alerted for this task
  }

  await redis.setex(key, 3600, '1');  // Cache for 1 hour
  return true;
}
```

## Troubleshooting

### Slack Messages Not Appearing

**1. Check Lambda logs**:
```bash
aws logs tail /aws/lambda/serve-analyze-slack-notifier-dev --follow
```

Look for errors like:
- `invalid_payload` → JSON format issue
- `channel_not_found` → Webhook was deleted
- `Timeout` → Webhook URL unreachable

**2. Test webhook manually**:
```bash
curl -X POST https://hooks.slack.com/services/YOUR/WEBHOOK/URL \
  -H 'Content-Type: application/json' \
  -d '{
    "text": "Test message from curl",
    "username": "Test Bot",
    "icon_emoji": ":rocket:"
  }'
```

**3. Verify Lambda has correct webhook URL**:
```bash
aws lambda get-function-configuration \
  --function-name serve-analyze-slack-notifier-dev \
  --query 'Environment.Variables.SLACK_WEBHOOK_URL'
```

### Messages Appearing in Wrong Channel

**Issue**: Webhook posts to wrong channel

**Cause**: Webhook was created for different channel

**Fix**: Create new webhook for correct channel, update Terraform variable

### Message Format Broken

**Issue**: Message appears as plain text, no formatting

**Cause**: Slack deprecated legacy attachments in your workspace

**Fix**: Update to Block Kit format:

```typescript
// New format (Block Kit)
{
  blocks: [
    {
      type: 'header',
      text: {
        type: 'plain_text',
        text: ':rotating_light: ECS Task Failed'
      }
    },
    {
      type: 'section',
      fields: [
        { type: 'mrkdwn', text: `*Environment:*\ndev` },
        { type: 'mrkdwn', text: `*Task ID:*\nabc12345` }
      ]
    }
  ]
}
```

## Cost

**Per failure notification**:
- Lambda invocation: $0.0000002
- Lambda execution: $0.000001 (100ms)
- SNS → Lambda: FREE
- Slack webhook: FREE

**Total**: ~$0.000001 per failure (essentially free)

## Comparison: Email vs Slack

| Feature | Email | Slack |
|---------|-------|-------|
| **Delivery Speed** | 1-2 minutes | <10 seconds |
| **Rich Formatting** | Limited | Full (colors, buttons, blocks) |
| **Mobile Notifications** | Yes | Yes (faster) |
| **Team Discussion** | Reply-all chaos | Threaded replies |
| **One-Click Actions** | No | Yes (View Logs button) |
| **Searchability** | Folder organization | Channel search + threads |
| **Alert Fatigue** | High (inbox spam) | Medium (can mute channel) |
| **Cost** | FREE | FREE |

**Recommendation**: Use **Slack for primary alerts** and **email as backup**.

## Next Steps

1. ✅ Create Slack webhook
2. ✅ Build Lambda
3. ✅ Configure Terraform with webhook URL
4. ✅ Deploy infrastructure
5. ✅ Test with intentional failure
6. Optional: Add campaign name to messages
7. Optional: Set up critical alerts channel
8. Optional: Configure on-call mentions

## Example: Complete Terraform Configuration

```hcl
# terraform.tfvars
environment                = "dev"
slack_webhook_url          = "https://hooks.slack.com/services/T00/B00/XXX"
failure_notification_email = "backup@example.com"  # Optional backup

# Both Slack AND email will receive notifications
```

**Result**: Team gets instant Slack alerts with one-click log access! 🎉
