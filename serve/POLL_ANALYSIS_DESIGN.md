# Poll Analysis Pipeline Design

## Overview

Extend the poll analysis pipeline to support both open-ended and structured (multiple choice) questions, with classification into predefined options and summarization.

## Architecture

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Serve API   │────▶│ S3 (CSV)    │     │ SQS Queue   │────▶│ Step        │────▶│ ECS Task    │
│             │     │             │     │ (metadata)  │     │ Functions   │     │             │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
      │                                        ▲
      │                                        │
      └──────── SQS message with options ──────┘
```

### Why SQS Instead of S3 Trigger

| S3 Trigger (current) | SQS Trigger (new) |
|---------------------|-------------------|
| Only knows filename | Full JSON payload with metadata |
| No question options | Options defined programmatically |
| No retry control | Built-in retry + DLQ |
| No callbacks | Webhook on complete/fail |

## Data Flow

### Inputs

**1. SQS Message (from Serve)**
```json
{
  "poll_id": "berkley-park-2025",
  "campaign_id": "berkley",
  "s3_bucket": "serve-analyze-data-prod",
  "s3_key": "input/berkley-park-2025.csv",

  "question_text": "Do you support the new park project?",
  "options": ["Yes", "No"],

  "callback": {
    "success_url": "https://serve.api/webhooks/complete",
    "failure_url": "https://serve.api/webhooks/failed"
  }
}
```

**2. CSV (same format as today)**
```csv
Contact Phone Number,Message Text,Sent At,Carrier
+15551234567,yes,2025-12-15T10:00:00Z,VERIZON
+15559876543,nope,2025-12-15T10:01:00Z,AT&T
+15555555555,traffic will be terrible,2025-12-15T10:02:00Z,T-MOBILE
```

**3. Results CSV (demographics)**
```csv
Contact Phone Number,voters_age,city_ward,voters_gender,residence_addresses_city
+15551234567,41,CITY 2,F,Berkley
+15559876543,35,CITY 1,M,Berkley
```

### Processing

```
              ┌─────────────────────────────────┐
              │ Load CSV + SQS metadata         │
              └───────────────┬─────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────┐
              │ Has options defined?            │
              └───────────────┬─────────────────┘
                              │
               ┌──────────────┴──────────────┐
               │ YES                         │ NO
               ▼                             ▼
┌──────────────────────────┐   ┌──────────────────────────┐
│ Classify responses into  │   │ All responses go to      │
│ predefined options       │   │ "Other" bucket           │
│ + "Other" bucket         │   │                          │
└────────────┬─────────────┘   └────────────┬─────────────┘
             │                              │
             └──────────────┬───────────────┘
                            │
                            ▼
              ┌─────────────────────────────────┐
              │ Summarize each bucket           │
              │ (LLM generates description)     │
              └───────────────┬─────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────┐
              │ Output results                  │
              └─────────────────────────────────┘
```

### Output

**Structured Question (has options)**
```json
{
  "poll_id": "berkley-park-2025",
  "question_text": "Do you support the new park project?",
  "options": ["Yes", "No"],
  "total_responses": 150,

  "results": [
    {
      "option": "Yes",
      "count": 65,
      "percentage": 43.3,
      "summary": "Supporters cite need for green space and family recreation areas"
    },
    {
      "option": "No",
      "count": 58,
      "percentage": 38.7,
      "summary": "Opposition focused on traffic concerns, parking, and cost to taxpayers"
    },
    {
      "option": "Other",
      "count": 27,
      "percentage": 18.0,
      "summary": "Responses about unrelated topics or unclear intent"
    }
  ],

  "demographics": {
    "by_age": {
      "18-34": {"Yes": 25, "No": 15, "Other": 8},
      "35-54": {"Yes": 22, "No": 25, "Other": 10},
      "55+":   {"Yes": 18, "No": 18, "Other": 9}
    }
  }
}
```

**Open-Ended Question (no options)**
```json
{
  "poll_id": "berkley-issues-2025",
  "question_text": "What issues matter most to you?",
  "options": [],
  "total_responses": 150,

  "results": [
    {
      "option": "Other",
      "count": 150,
      "percentage": 100.0,
      "summary": "Top concerns: road conditions (25%), property taxes (22%), school funding (18%), public safety (15%). Sentiment is mostly negative, with frustration about lack of progress on infrastructure."
    }
  ]
}
```

## Classification Logic

### Step 1: Classify Each Response

LLM classifies each `Message Text` into one of the predefined options or "Other":

```
Options: ["Yes", "No"]

"yes"                    → Yes
"Yes!"                   → Yes
"absolutely"             → Yes
"support it"             → Yes
"no"                     → No
"nope"                   → No
"never"                  → No
"against it"             → No
"yes but worried about cost" → Yes (with reason)
"no, traffic is bad"     → No (with reason)
"what about the library?" → Other (off-topic)
"maybe"                  → Other (unclear)
"stop"                   → filtered out (opt-out)
```

### Step 2: Extract Reasons

When a response includes a reason, extract it:

```
"no, traffic will be terrible"
  → option: No
  → reason: "traffic will be terrible"

"yes because we need more parks"
  → option: Yes
  → reason: "we need more parks"
```

### Step 3: Summarize Each Bucket

Group all reasons by option and summarize:

```
Yes reasons:
- "we need more parks"
- "great for kids"
- "love green spaces"
- "good for property values"

LLM → "Supporters cite need for green space and family recreation areas"
```

## SQS Message Schema

```json
{
  "poll_id": "string (required)",
  "campaign_id": "string (optional, defaults to poll_id)",
  "s3_bucket": "string (required)",
  "s3_key": "string (required)",

  "question_text": "string (required)",
  "options": ["string"] | [],

  "processing_options": {
    "include_demographics": true,
    "demographic_fields": ["age", "ward", "gender"]
  },

  "callback": {
    "success_url": "string (optional)",
    "failure_url": "string (optional)"
  }
}
```

### Examples

**Structured (Yes/No):**
```json
{
  "poll_id": "park-vote-2025",
  "s3_key": "input/park-vote.csv",
  "question_text": "Do you support the new park?",
  "options": ["Yes", "No"]
}
```

**Structured (Multiple Choice):**
```json
{
  "poll_id": "priority-poll-2025",
  "s3_key": "input/priorities.csv",
  "question_text": "What is your top priority?",
  "options": ["Roads", "Schools", "Safety", "Taxes"]
}
```

**Open-Ended:**
```json
{
  "poll_id": "feedback-2025",
  "s3_key": "input/feedback.csv",
  "question_text": "What issues matter most to you?",
  "options": []
}
```

## Infrastructure Changes

### New SQS Queue

```hcl
resource "aws_sqs_queue" "v2_trigger" {
  name                       = "serve-analyze-v2-trigger-${var.environment}"
  visibility_timeout_seconds = 120  # Must exceed Lambda timeout
  message_retention_seconds  = 86400  # 1 day
  receive_wait_time_seconds  = 20  # Long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.v2_trigger_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_sqs_queue" "v2_trigger_dlq" {
  name                      = "serve-analyze-v2-trigger-dlq-${var.environment}"
  message_retention_seconds = 1209600  # 14 days
}
```

> Note: Standard queue (not FIFO) for simplicity. If ordering is needed, switch to FIFO with message groups.

### Lambda Event Source Mapping

```hcl
resource "aws_lambda_event_source_mapping" "sqs_trigger" {
  event_source_arn = aws_sqs_queue.poll_analysis_trigger.arn
  function_name    = aws_lambda_function.pipeline_trigger.arn
  batch_size       = 1
}
```

### Step Functions DLQ on Failure

If ECS task fails after Lambda has already deleted the SQS message, Step Functions sends original payload to DLQ:

```json
{
  "States": {
    "RunECSTask": {
      "Type": "Task",
      "Resource": "arn:aws:states:::ecs:runTask.sync",
      "Retry": [
        {
          "ErrorEquals": ["States.TaskFailed"],
          "MaxAttempts": 2,
          "BackoffRate": 2
        }
      ],
      "Catch": [
        {
          "ErrorEquals": ["States.ALL"],
          "ResultPath": "$.error",
          "Next": "SendToDLQ"
        }
      ],
      "Next": "Success"
    },
    "SendToDLQ": {
      "Type": "Task",
      "Resource": "arn:aws:states:::sqs:sendMessage",
      "Parameters": {
        "QueueUrl": "${dlq_url}",
        "MessageBody": {
          "original_input.$": "$$.Execution.Input",
          "error.$": "$.error",
          "execution_id.$": "$$.Execution.Id"
        }
      },
      "Next": "NotifyFailure"
    }
  }
}
```

## Python Implementation

### Classifier

```python
from dataclasses import dataclass
from shared.llm_gemini import GeminiClient

@dataclass
class ClassificationResult:
    option: str
    reason: str | None
    confidence: float

class ResponseClassifier:
    def __init__(self, options: list[str]):
        self.options = options
        self.llm = GeminiClient()

    async def classify(self, response_text: str, question_text: str) -> ClassificationResult:
        if not self.options:
            return ClassificationResult(option="Other", reason=response_text, confidence=1.0)

        prompt = f"""
Question: {question_text}
Valid options: {', '.join(self.options)}

Classify this response into one of the options above, or "Other" if it doesn't fit.
Also extract any reason/explanation given.

Response: "{response_text}"

Reply in JSON:
{{"option": "Yes|No|Other", "reason": "extracted reason or null", "confidence": 0.0-1.0}}
"""
        result = await self.llm.generate_json(prompt)
        return ClassificationResult(**result)
```

### Summarizer

```python
class BucketSummarizer:
    def __init__(self):
        self.llm = GeminiClient()

    async def summarize(self, option: str, reasons: list[str], question_text: str) -> str:
        if not reasons:
            return f"No specific reasons provided for {option}"

        prompt = f"""
Question: {question_text}
These people responded "{option}". Here are their reasons:

{chr(10).join(f'- {r}' for r in reasons[:50])}

Summarize in 1-2 sentences what this group is saying.
"""
        return await self.llm.generate_text(prompt)
```

### Main Processor

```python
@dataclass
class PollAnalysisResult:
    poll_id: str
    question_text: str
    options: list[str]
    total_responses: int
    results: list[dict]
    demographics: dict | None

class PollAnalyzer:
    def __init__(self, poll_metadata: dict):
        self.poll_id = poll_metadata["poll_id"]
        self.question_text = poll_metadata["question_text"]
        self.options = poll_metadata.get("options", [])

        self.classifier = ResponseClassifier(self.options)
        self.summarizer = BucketSummarizer()

    async def analyze(self, responses: list[dict]) -> PollAnalysisResult:
        # Step 1: Classify all responses
        classified = {}
        for opt in self.options + ["Other"]:
            classified[opt] = {"count": 0, "reasons": []}

        for response in responses:
            text = response["message_text"]

            # Skip opt-outs
            if text.lower().strip() in ["stop", "unsubscribe", "quit"]:
                continue

            result = await self.classifier.classify(text, self.question_text)

            option = result.option if result.option in self.options else "Other"
            classified[option]["count"] += 1
            if result.reason:
                classified[option]["reasons"].append(result.reason)

        # Step 2: Summarize each bucket
        total = sum(c["count"] for c in classified.values())
        results = []

        for option, data in classified.items():
            if data["count"] == 0:
                continue

            summary = await self.summarizer.summarize(
                option, data["reasons"], self.question_text
            )

            results.append({
                "option": option,
                "count": data["count"],
                "percentage": round(data["count"] / total * 100, 1) if total > 0 else 0,
                "summary": summary
            })

        return PollAnalysisResult(
            poll_id=self.poll_id,
            question_text=self.question_text,
            options=self.options,
            total_responses=total,
            results=results,
            demographics=None  # TODO: add cross-tabs
        )
```

## Migration Path

### Phase 1: Add SQS Infrastructure
- Deploy SQS queue + DLQ
- Update Lambda to handle SQS events
- Keep S3 trigger as fallback

### Phase 2: Update Pipeline
- Add classification + summarization logic
- Support `options` in metadata
- Backwards compatible (no options = open-ended = current behavior)

### Phase 3: Integrate with Serve
- Serve sends SQS messages with options
- Remove S3 trigger once stable

## API Contract

### Serve → SQS

```typescript
interface PollAnalysisRequest {
  poll_id: string;
  campaign_id?: string;
  s3_bucket: string;
  s3_key: string;
  question_text: string;
  options: string[];  // empty array for open-ended
  callback?: {
    success_url?: string;
    failure_url?: string;
  };
}
```

### Pipeline → Serve (callback)

```typescript
interface PollAnalysisResponse {
  poll_id: string;
  status: "success" | "failed";
  question_text: string;
  total_responses: number;
  results: Array<{
    option: string;
    count: number;
    percentage: number;
    summary: string;
  }>;
  error?: string;
}
```

## Summary

| Aspect | Current (V1) | New (V2) |
|--------|--------------|----------|
| Trigger | S3 upload (`input/`) | SQS message |
| Question types | Open-ended only | Open-ended + structured |
| Processing | Clustering + themes | Classification + summarization |
| Options | None | Defined in SQS message |
| Output | Clusters with quotes | Options with counts + summaries |
| Retry | None | SQS + Step Functions DLQ |
| S3 Path | `input/` (triggers V1) | `input_v2/` (no S3 trigger) |

## Implementation Files

### Infrastructure (Terraform)

**`infrastructure/modules/serve-analyze-fargate/main.tf`**
- `aws_sqs_queue.v2_trigger` - SQS queue for V2 triggers
- `aws_sqs_queue.v2_trigger_dlq` - Dead letter queue for failed V2 messages
- `aws_lambda_event_source_mapping.sqs_trigger` - Lambda trigger from SQS
- Lambda environment variables include `V2_DLQ_URL`

**`infrastructure/modules/serve-analyze-fargate/step-function-definition.json`**
- Updated to include `CheckV2DLQ` choice state
- `SendToDLQ` state for V2 failures - preserves original request for retry

### Lambda Trigger

**`infrastructure/modules/serve-analyze-fargate/lambda-trigger/index.ts`**
- `isSQSEvent()` - Detects SQS events
- `processV1Pipeline()` - Handles S3 triggers (clustering)
- `processV2Pipeline()` - Handles SQS triggers (classification)
- Routes based on event type, sets `PIPELINE_MODE` env var

### Pipeline Code

**`serve/v1_pipeline/entrypoint.sh`**
- Reads `PIPELINE_MODE` env var (default: `cluster`)
- Passes V2-specific args: `--poll-id`, `--question-text`, `--options-json`, callbacks

**`serve/v1_pipeline/scripts/run_pipeline.py`**
- `--mode` arg: `cluster` or `classify`
- `--poll-id`, `--question-text`, `--options-json` for V2
- `--callback-success-url`, `--callback-failure-url` for webhooks

**`serve/v1_pipeline/pipeline/v2_classifier.py`**
- `V2ClassificationPipeline` - Main orchestrator
- `ResponseClassifier` - Parallel LLM classification (100 concurrent)
- `BucketSummarizer` - Summarizes each option bucket
- `V2PipelineResult` - Output data class with `to_dict()`

## Usage Examples

### Send V2 Message (from Serve API)

```typescript
import { SQSClient, SendMessageCommand } from '@aws-sdk/client-sqs';

const sqsClient = new SQSClient({});

await sqsClient.send(new SendMessageCommand({
  QueueUrl: process.env.V2_TRIGGER_QUEUE_URL,
  MessageBody: JSON.stringify({
    poll_id: "park-vote-2025",
    campaign_id: "berkley",
    s3_bucket: "serve-analyze-data-prod",
    s3_key: "input_v2/park-vote-2025.csv",  // Note: input_v2/ to avoid V1 trigger
    question_text: "Do you support the new park?",
    options: ["Yes", "No"],
    callback: {
      success_url: "https://api.goodparty.org/webhooks/poll-complete",
      failure_url: "https://api.goodparty.org/webhooks/poll-failed"
    }
  })
}));
```

### Local Testing

```bash
# V1 Pipeline (clustering)
uv run serve/v1_pipeline/scripts/run_pipeline.py --campaign berkley --mode cluster

# V2 Pipeline (classification)
uv run serve/v1_pipeline/scripts/run_pipeline.py \
  --campaign park-vote-2025 \
  --mode classify \
  --poll-id park-vote-2025 \
  --question-text "Do you support the new park?" \
  --options-json '["Yes", "No"]' \
  --save-results output/park-vote-results.json
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `PIPELINE_MODE` | `cluster` or `classify` | Yes |
| `POLL_ID` | Poll identifier (V2) | V2 only |
| `QUESTION_TEXT` | Question text (V2) | V2 only |
| `OPTIONS_JSON` | JSON array of options (V2) | V2 only |
| `CALLBACK_SUCCESS_URL` | Webhook for success (V2) | No |
| `CALLBACK_FAILURE_URL` | Webhook for failure (V2) | No |
| `S3_INPUT_PATH` | S3 path to CSV | Yes |
| `S3_OUTPUT_PATH` | S3 path for results | Yes |
| `CAMPAIGN_NAME` | Campaign identifier | Yes |
