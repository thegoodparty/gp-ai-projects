# SQS-Triggered Pipeline Design

## Overview

Replace S3 trigger with SQS trigger to enable rich metadata passing for question-type-aware processing.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              SERVE API / WEBAPP                               │
└─────────────────────────────────┬────────────────────────────────────────────┘
                                  │
                    1. Upload CSV to S3
                    2. Send SQS message with metadata
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                                                                              │
│  ┌─────────────┐              ┌─────────────────────────────────────────┐   │
│  │             │              │  SQS: serve-analyze-trigger-{env}       │   │
│  │  S3 Bucket  │              │                                         │   │
│  │  (CSV data) │              │  Message:                               │   │
│  │             │              │  {                                      │   │
│  └─────────────┘              │    "poll_id": "abc-123",                │   │
│                               │    "s3_key": "input/abc-123.csv",       │   │
│                               │    "poll_metadata": {...}               │   │
│                               │  }                                      │   │
│                               └──────────────────┬──────────────────────┘   │
│                                                  │                          │
│                                                  ▼                          │
│                               ┌─────────────────────────────────────────┐   │
│                               │  Lambda: serve-analyze-trigger-{env}    │   │
│                               │                                         │   │
│                               │  • Validates message                    │   │
│                               │  • Checks S3 file exists                │   │
│                               │  • Starts Step Functions execution      │   │
│                               └──────────────────┬──────────────────────┘   │
│                                                  │                          │
│                                                  ▼                          │
│                               ┌─────────────────────────────────────────┐   │
│                               │  Step Functions State Machine           │   │
│                               │                                         │   │
│                               │  • Runs ECS Task with metadata          │   │
│                               │  • Handles retries                      │   │
│                               │  • Publishes completion/failure         │   │
│                               └──────────────────┬──────────────────────┘   │
│                                                  │                          │
│                                                  ▼                          │
│                               ┌─────────────────────────────────────────┐   │
│                               │  ECS Fargate Task                       │   │
│                               │                                         │   │
│                               │  Environment:                           │   │
│                               │  • POLL_ID=abc-123                      │   │
│                               │  • S3_INPUT_KEY=input/abc-123.csv       │   │
│                               │  • POLL_METADATA_JSON={...}             │   │
│                               └─────────────────────────────────────────┘   │
│                                                                              │
│                                         AWS                                  │
└──────────────────────────────────────────────────────────────────────────────┘
```

## SQS Message Schema

```json
{
  "poll_id": "berkeley-mayor-2025-q4",
  "campaign_id": "berkeley",
  "s3_bucket": "serve-analyze-data-prod",
  "s3_key": "input/berkeley-mayor-2025-q4.csv",

  "poll_metadata": {
    "poll_name": "Berkeley Mayor Approval Q4 2025",
    "created_at": "2025-12-15T10:00:00Z",

    "questions": [
      {
        "question_id": "Q1",
        "question_type": "rating_scale",
        "question_text": "How would you rate the mayor's performance? (1-5)",
        "scale_min": 1,
        "scale_max": 5,
        "scale_labels": {
          "1": "Very Poor",
          "5": "Excellent"
        }
      },
      {
        "question_id": "Q2",
        "question_type": "multiple_choice",
        "question_text": "What is your top priority for the city?",
        "options": [
          {"value": "1", "label": "Road Infrastructure"},
          {"value": "2", "label": "Public Schools"},
          {"value": "3", "label": "Public Safety"},
          {"value": "4", "label": "Housing Affordability"},
          {"value": "5", "label": "Other"}
        ],
        "allow_multiple": false
      },
      {
        "question_id": "Q3",
        "question_type": "open_ended",
        "question_text": "What other issues would you like the mayor to address?"
      },
      {
        "question_id": "Q4",
        "question_type": "mixed",
        "question_text": "Do you support the new housing development? Please explain.",
        "options": [
          {"value": "Y", "label": "Yes"},
          {"value": "N", "label": "No"},
          {"value": "U", "label": "Unsure"}
        ],
        "follow_up_required": true
      }
    ],

    "csv_column_mapping": {
      "phone_column": "Contact Phone Number",
      "Q1_column": "rating_response",
      "Q2_column": "priority_response",
      "Q3_column": "open_response",
      "Q4_column": "housing_response"
    }
  },

  "processing_options": {
    "skip_clustering_for_structured": true,
    "cross_tab_demographics": ["age_group", "location"],
    "min_cluster_size": 5
  },

  "callback": {
    "success_webhook": "https://api.goodparty.org/webhooks/poll-complete",
    "failure_webhook": "https://api.goodparty.org/webhooks/poll-failed"
  }
}
```

## CSV Format (Matching Message Schema)

```csv
Contact Phone Number,rating_response,priority_response,open_response,housing_response,age_group,location
555-1234,4,2,Property taxes too high,Y - brings jobs to the area,35-54,Downtown
555-5678,2,1,Roads are falling apart,N - too much traffic,55+,Northside
555-9012,5,3,,U - need more info,18-34,Southside
```

## Terraform Changes

### New SQS Queue

```hcl
# infrastructure/modules/serve-analyze-fargate/sqs.tf

resource "aws_sqs_queue" "pipeline_trigger" {
  name                       = "serve-analyze-trigger-${var.environment}"
  visibility_timeout_seconds = 900  # 15 min (must exceed Lambda + Step Functions startup)
  message_retention_seconds  = 86400  # 1 day
  receive_wait_time_seconds  = 20  # Long polling

  # Prevent duplicate processing
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_queue                  = true
  fifo_throughput_limit       = "perMessageGroupId"

  tags = {
    Environment = var.environment
    Purpose     = "Pipeline trigger with metadata"
  }
}

resource "aws_sqs_queue" "pipeline_trigger_dlq" {
  name = "serve-analyze-trigger-dlq-${var.environment}.fifo"
  fifo_queue = true
  message_retention_seconds = 1209600  # 14 days

  tags = {
    Environment = var.environment
    Purpose     = "Dead letter queue for failed triggers"
  }
}

resource "aws_sqs_queue_redrive_policy" "pipeline_trigger" {
  queue_url = aws_sqs_queue.pipeline_trigger.id
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.pipeline_trigger_dlq.arn
    maxReceiveCount     = 3  # Retry 3 times before DLQ
  })
}
```

### Updated Lambda Trigger

```hcl
# Update Lambda to use SQS trigger instead of S3

resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn                   = aws_sqs_queue.pipeline_trigger.arn
  function_name                      = aws_lambda_function.pipeline_trigger.arn
  batch_size                         = 1  # Process one poll at a time
  maximum_batching_window_in_seconds = 0  # No batching delay

  # Only delete message after successful processing
  function_response_types = ["ReportBatchItemFailures"]
}

# Remove S3 trigger
# resource "aws_s3_bucket_notification" "pipeline_trigger" { ... }  # DELETE THIS
```

### Lambda Handler Update

```typescript
// infrastructure/modules/serve-analyze-fargate/lambda-trigger/index.ts

import { SQSEvent, SQSBatchResponse, SQSBatchItemFailure } from 'aws-lambda';
import { SFNClient, StartExecutionCommand } from '@aws-sdk/client-sfn';

interface PollTriggerMessage {
  poll_id: string;
  campaign_id: string;
  s3_bucket: string;
  s3_key: string;
  poll_metadata: {
    questions: Array<{
      question_id: string;
      question_type: 'open_ended' | 'multiple_choice' | 'rating_scale' | 'mixed';
      question_text: string;
      options?: Array<{ value: string; label: string }>;
    }>;
    csv_column_mapping?: Record<string, string>;
  };
  processing_options?: Record<string, unknown>;
  callback?: {
    success_webhook?: string;
    failure_webhook?: string;
  };
}

export async function handler(event: SQSEvent): Promise<SQSBatchResponse> {
  const sfnClient = new SFNClient({});
  const batchItemFailures: SQSBatchItemFailure[] = [];

  for (const record of event.Records) {
    try {
      const message: PollTriggerMessage = JSON.parse(record.body);

      // Validate required fields
      if (!message.poll_id || !message.s3_key) {
        throw new Error('Missing required fields: poll_id and s3_key');
      }

      // Start Step Functions execution with full metadata
      const executionInput = {
        poll_id: message.poll_id,
        campaign_id: message.campaign_id || message.poll_id,
        s3_bucket: message.s3_bucket || process.env.S3_OUTPUT_BUCKET,
        s3_key: message.s3_key,
        poll_metadata: message.poll_metadata || { questions: [] },
        processing_options: message.processing_options || {},
        callback: message.callback || {},
        triggered_at: new Date().toISOString(),
        sqs_message_id: record.messageId
      };

      await sfnClient.send(new StartExecutionCommand({
        stateMachineArn: process.env.STATE_MACHINE_ARN,
        name: `poll-${message.poll_id}-${Date.now()}`,
        input: JSON.stringify(executionInput)
      }));

      console.log(`Started execution for poll: ${message.poll_id}`);

    } catch (error) {
      console.error(`Failed to process message ${record.messageId}:`, error);
      batchItemFailures.push({ itemIdentifier: record.messageId });
    }
  }

  return { batchItemFailures };
}
```

## ECS Task Environment Variables

The Step Functions state machine passes metadata to ECS via environment variable overrides:

```json
{
  "containerOverrides": [
    {
      "name": "serve-analyze",
      "environment": [
        {"name": "POLL_ID", "value.$": "$.poll_id"},
        {"name": "CAMPAIGN_ID", "value.$": "$.campaign_id"},
        {"name": "S3_INPUT_BUCKET", "value.$": "$.s3_bucket"},
        {"name": "S3_INPUT_KEY", "value.$": "$.s3_key"},
        {"name": "POLL_METADATA_JSON", "value.$": "States.JsonToString($.poll_metadata)"},
        {"name": "PROCESSING_OPTIONS_JSON", "value.$": "States.JsonToString($.processing_options)"},
        {"name": "CALLBACK_WEBHOOK_SUCCESS", "value.$": "$.callback.success_webhook"},
        {"name": "CALLBACK_WEBHOOK_FAILURE", "value.$": "$.callback.failure_webhook"}
      ]
    }
  ]
}
```

## Python Pipeline Changes

```python
# serve/v1_pipeline/pipeline/orchestrator.py

import os
import json

class V1PipelineOrchestrator:
    def __init__(self, config_path: str | None = None):
        # ... existing init ...

        # NEW: Load poll metadata from environment
        self.poll_metadata = self._load_poll_metadata_from_env()

    def _load_poll_metadata_from_env(self) -> PollMetadata | None:
        """Load poll metadata from environment variable (set by Step Functions)"""
        metadata_json = os.environ.get('POLL_METADATA_JSON')
        if not metadata_json:
            logger.info("No POLL_METADATA_JSON found - using legacy open-ended mode")
            return None

        try:
            raw = json.loads(metadata_json)
            return PollMetadata.from_dict(raw)
        except Exception as e:
            logger.warning(f"Failed to parse poll metadata: {e}")
            return None

    async def run_pipeline(self, campaign_name: str) -> PipelineResult:
        # ... existing consolidation ...

        # NEW: Route by question type if metadata available
        if self.poll_metadata:
            router = QuestionTypeRouter(self.config)
            results = await router.route_and_process(messages, self.poll_metadata)
        else:
            # Legacy: treat all as open-ended
            results = await self._run_clustering_stage(messages, campaign_name)
```

## Migration Strategy

### Phase 1: Add SQS (Keep S3 Trigger)
- Deploy SQS queue and update Lambda to handle both triggers
- S3 trigger → creates minimal SQS-like payload internally
- SQS trigger → uses full metadata
- Zero breaking changes

### Phase 2: Update Serve API
- Serve API starts sending SQS messages after S3 upload
- Include poll metadata in message
- S3 trigger becomes backup/fallback

### Phase 3: Remove S3 Trigger
- Once all polls go through SQS, remove S3 trigger
- S3 bucket retains lifecycle rules for data management

## Serve API Integration Example

```typescript
// In gp-api or Serve backend

async function triggerPollAnalysis(poll: Poll, csvS3Key: string) {
  const sqsClient = new SQSClient({});

  const message: PollTriggerMessage = {
    poll_id: poll.id,
    campaign_id: poll.campaignId,
    s3_bucket: process.env.ANALYZE_BUCKET,
    s3_key: csvS3Key,
    poll_metadata: {
      poll_name: poll.name,
      questions: poll.questions.map(q => ({
        question_id: q.id,
        question_type: q.type,
        question_text: q.text,
        options: q.options
      })),
      csv_column_mapping: poll.csvMapping
    },
    callback: {
      success_webhook: `${process.env.API_URL}/webhooks/poll-analysis-complete`,
      failure_webhook: `${process.env.API_URL}/webhooks/poll-analysis-failed`
    }
  };

  await sqsClient.send(new SendMessageCommand({
    QueueUrl: process.env.ANALYZE_TRIGGER_QUEUE_URL,
    MessageBody: JSON.stringify(message),
    MessageGroupId: poll.campaignId,  // FIFO deduplication
    MessageDeduplicationId: `${poll.id}-${Date.now()}`
  }));
}
```

## Benefits Summary

| Current (S3 Trigger) | New (SQS Trigger) |
|---------------------|-------------------|
| Filename = only context | Full JSON metadata |
| Implicit question type | Explicit question types |
| No retry control | Configurable retries + DLQ |
| Race conditions possible | FIFO ordering per campaign |
| Hard to test | Easy to send test messages |
| No callbacks | Webhook callbacks on complete/fail |
